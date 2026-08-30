import asyncio
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cognee
from cognee import SearchType

from .config import Config
from .tapes_client import TapesClient, parse_ts
from .transcript import build_transcript, get_status

logger = logging.getLogger(__name__)


def apply_storage_isolation(config: Config) -> None:
    """Force cognee storage under ``config.storage_root`` when configured.

    Guards against globally-exported cognee storage env vars silently pointing
    the cassette at a shared store.
    """
    if not config.storage_root:
        return
    root = Path(config.storage_root).expanduser().resolve()
    cognee.config.data_root_directory(str(root / "data"))
    cognee.config.system_root_directory(str(root / "system"))
    logger.info("Cognee storage isolated under %s", root)


@dataclass
class SyncStatus:
    state: str = "idle"  # idle | running | completed | failed
    started_at: str | None = None
    finished_at: str | None = None
    fetched: int = 0
    ingested: int = 0
    unchanged: int = 0
    skipped: int = 0
    error: str | None = None
    last_synced_at: str | None = None
    dataset: str = ""

    def snapshot(self) -> dict:
        return asdict(self)


@dataclass
class _State:
    """On-disk sync state: per-session content hashes + incremental checkpoint."""

    sessions: dict = field(default_factory=dict)
    last_synced_at: str | None = None
    pending_cognify: bool = False


class Syncer:
    """Pulls sessions from tapes and ingests them into a cognee dataset.

    Single-flight: the cassette is one async process, so an ``asyncio`` task
    handle (not a file lock) is what prevents overlapping runs.
    """

    def __init__(self, config: Config, tapes: TapesClient):
        self._config = config
        self._tapes = tapes
        self._task: asyncio.Task | None = None
        self.status = SyncStatus(dataset=config.dataset_name)

    # -- state persistence -------------------------------------------------

    def _load_state(self) -> _State:
        path = self._config.state_path
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                return _State(
                    sessions=raw.get("sessions", {}),
                    last_synced_at=raw.get("last_synced_at"),
                    pending_cognify=raw.get("pending_cognify", False),
                )
            except (json.JSONDecodeError, TypeError):
                logger.warning("Unreadable state file %s — starting fresh.", path)
        return _State()

    def _save_state(self, state: _State) -> None:
        self._config.state_path.write_text(json.dumps(asdict(state), indent=2))

    # -- sync --------------------------------------------------------------

    def start(self, full: bool = False) -> bool:
        """Kick off a background sync; returns False if one is already running."""
        if self.is_running():
            return False
        self._task = asyncio.create_task(self.run(full=full))
        return True

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run(self, full: bool = False) -> SyncStatus:
        status = SyncStatus(
            state="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            dataset=self._config.dataset_name,
        )
        self.status = status
        state = self._load_state()
        checkpoint = None if full else state.last_synced_at

        try:
            items = await self._tapes.list_sessions(since=checkpoint)
            status.fetched = len(items)
            logger.info("Fetched %d session list item(s) (checkpoint=%s).", len(items), checkpoint)

            latest_completed: datetime | None = None
            for item in items:
                session_id = item.get("id")
                if not session_id:
                    status.skipped += 1
                    continue

                try:
                    export = await self._tapes.export_session(session_id)
                except Exception as exc:  # noqa: BLE001 — skip and continue with the rest
                    logger.error("Failed to export session %s: %s — skipping.", session_id, exc)
                    status.skipped += 1
                    continue

                if get_status(export) != "completed":
                    # Incomplete sessions never advance the checkpoint: their
                    # last_seen_at bumps again when they complete, so the next
                    # incremental run picks them up.
                    status.skipped += 1
                    continue

                text = build_transcript(export)
                if not text:
                    status.skipped += 1
                    continue

                text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if state.sessions.get(session_id) == text_hash:
                    status.unchanged += 1
                else:
                    logger.info("Ingesting session %s.", session_id)
                    await cognee.add(data=text, dataset_name=self._config.dataset_name)
                    state.sessions[session_id] = text_hash
                    state.pending_cognify = True
                    status.ingested += 1
                    self._save_state(state)  # incremental — protects progress mid-run

                if last_seen_at := item.get("last_seen_at"):
                    try:
                        seen = parse_ts(last_seen_at)
                    except ValueError:
                        continue
                    if latest_completed is None or seen > latest_completed:
                        latest_completed = seen

            if state.pending_cognify:
                await cognee.cognify(datasets=[self._config.dataset_name])
                state.pending_cognify = False
                self._save_state(state)
                logger.info("Cognify run complete.")

            if latest_completed is not None:
                state.last_synced_at = latest_completed.isoformat()
                self._save_state(state)

            status.state = "completed"
        except Exception as exc:  # noqa: BLE001 — surfaced via status, not a crashed task
            logger.exception("Sync failed.")
            status.state = "failed"
            status.error = str(exc)
        finally:
            status.finished_at = datetime.now(timezone.utc).isoformat()
            status.last_synced_at = state.last_synced_at

        return status


# -- search ------------------------------------------------------------------


def _jsonable(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


async def search(config: Config, query: str, search_type: str, top_k: int) -> list:
    try:
        query_type = SearchType[search_type.upper()]
    except KeyError:
        valid = ", ".join(t.name for t in SearchType)
        raise ValueError(f"Unknown search_type {search_type!r}. Valid values: {valid}") from None

    results = await cognee.search(
        query_type=query_type,
        query_text=query,
        datasets=[config.dataset_name],
        top_k=top_k,
    )
    return [_jsonable(result) for result in results]
