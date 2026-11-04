import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cognee
from cognee import SearchType
from fastapi.encoders import jsonable_encoder

from .config import Config
from .tapes_client import TapesClient, parse_ts
from .transcript import build_transcript, get_status

try:  # cognee >= 1.6 raises this for a dataset that has data but no graph yet
    from cognee.modules.retrieval.exceptions.exceptions import NoDataError
except ImportError:  # pragma: no cover — moved in a future cognee; nothing to catch
    NoDataError = None

_NOTHING_SEARCHABLE = (NoDataError,) if NoDataError is not None else ()

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


@contextlib.contextmanager
def _run_lock(path: Path):
    """Non-blocking cross-process lock; yields whether it was acquired.

    The in-process task guard stops overlapping runs inside one cassette; this
    also stops the running service and a cron ``cognee-tapes-sync`` from
    syncing against the same state file at once. The OS drops the lock when
    the holder exits, so a crashed run never leaves it stuck.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")  # noqa: SIM115 — held for the whole run
    try:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        handle.close()


@dataclass
class SyncStatus:
    state: str = "idle"  # idle | running | completed | failed | cancelled | busy
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
    handle (not a file lock) is what prevents overlapping runs. Every sync,
    including one a caller waits on, goes through ``start()`` and that one
    task; the check and the ``create_task`` have no ``await`` between them, so
    two requests can never both start a run against the same state file.
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
        """Write the state atomically: a crash mid-write keeps the previous file."""
        path = self._config.state_path
        payload = json.dumps(asdict(state), indent=2)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # -- sync --------------------------------------------------------------

    def start(self, full: bool = False) -> bool:
        """Kick off a background sync; returns False if one is already running."""
        if self.is_running():
            return False
        self._task = asyncio.create_task(self._run(full=full))
        return True

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def wait(self) -> SyncStatus:
        """Wait for the current (or last) sync and return its status."""
        if self._task is None:
            return self.status
        return await asyncio.shield(self._task)

    async def shutdown(self) -> None:
        """Cancel a running sync and wait for it, before the tapes client closes."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self, full: bool = False) -> SyncStatus:
        lock_path = self._config.state_path.with_name(self._config.state_path.name + ".lock")
        with _run_lock(lock_path) as acquired:
            if not acquired:
                now = datetime.now(timezone.utc).isoformat()
                self.status = SyncStatus(
                    state="busy",
                    started_at=now,
                    finished_at=now,
                    dataset=self._config.dataset_name,
                    error=f"another sync process holds {lock_path}",
                )
                logger.info("Sync skipped: another process is syncing (%s).", lock_path)
                return self.status
            return await self._sync(full)

    async def _sync(self, full: bool) -> SyncStatus:
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
        except asyncio.CancelledError:
            # Shutdown mid-sync: progress so far is already saved per session.
            status.state = "cancelled"
            raise
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
    """JSON-safe form of one search result.

    cognee 1.x returns dicts carrying ``UUID`` dataset ids (with backend access
    control on, the default) or pydantic models; ``jsonable_encoder`` turns those
    into plain JSON. ``str()`` is only the last resort for anything it rejects.
    """
    try:
        return jsonable_encoder(value)
    except (TypeError, ValueError):
        return str(value)


async def search(config: Config, query: str, search_type: str, top_k: int) -> list:
    try:
        query_type = SearchType[search_type.upper()]
    except KeyError:
        valid = ", ".join(t.name for t in SearchType)
        raise ValueError(f"Unknown search_type {search_type!r}. Valid values: {valid}") from None

    try:
        results = await cognee.search(
            query_type=query_type,
            query_text=query,
            datasets=[config.dataset_name],
            top_k=top_k,
        )
    except _NOTHING_SEARCHABLE as exc:
        # Before the first cognify completes (a fresh install, or a failed
        # cognify still pending) there is nothing to search: an empty result,
        # not a server error.
        logger.info("Nothing searchable yet in %s: %s", config.dataset_name, exc)
        return []
    return [_jsonable(result) for result in results]
