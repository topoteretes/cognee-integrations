"""Walks chosen folders and feeds new/changed files into the adapter.

Indexing is incremental: a file is re-added only when its mtime moved past
what the catalog recorded, so re-running "Index" after editing three notes
uploads three files, not the whole tree. One indexing run at a time; the
status dict is what the macOS app polls to render progress.
"""

from __future__ import annotations

import asyncio
import traceback
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .config import SKIP_DIR_NAMES, Settings

BATCH_SIZE = 20

_FRESH_STATUS: dict[str, Any] = {
    "state": "idle",
    "total": 0,
    "done": 0,
    "skipped": 0,
    "last_skip": "",
    "error": "",
}


def discover_files(roots: list[str], settings: Settings) -> list[Path]:
    """All indexable files under ``roots``: allowed extension, size cap,
    hidden files and throwaway directories (node_modules, .git, ...) skipped."""
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        base = Path(root).expanduser()
        if base.is_file():
            candidates = [base]
        elif base.is_dir():
            candidates = _walk(base)
        else:
            continue
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            if path.suffix.lower() not in settings.extensions:
                continue
            try:
                if path.stat().st_size > settings.max_file_size:
                    continue
            except OSError:
                continue
            found.append(path)
    return found


def _walk(base: Path) -> list[Path]:
    out: list[Path] = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return out
    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue
        if entry.is_dir():
            if name not in SKIP_DIR_NAMES:
                out.extend(_walk(entry))
        elif entry.is_file():
            out.append(entry)
    return out


class Indexer:
    def __init__(self, adapter: Any, catalog: Catalog, settings: Settings) -> None:
        self._adapter = adapter
        self._catalog = catalog
        self._settings = settings
        self._task: asyncio.Task | None = None
        self.status: dict[str, Any] = dict(_FRESH_STATUS)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, roots: list[str]) -> bool:
        """Kick off a background indexing run. Returns False if one is running."""
        if self.running:
            return False
        self._catalog.add_roots(roots)
        # status flips before the task is scheduled, so a poll right after the
        # 202 never sees a stale "idle" from the previous run
        self.status = {**_FRESH_STATUS, "state": "scanning"}
        self._task = asyncio.get_running_loop().create_task(self._run(self._catalog.roots))
        return True

    async def _run(self, roots: list[str]) -> None:
        try:
            files = discover_files(roots, self._settings)
            stats = {p: p.stat() for p in files}
            todo = [p for p in files if self._catalog.needs_index(str(p), stats[p].st_mtime)]
            self.status.update(state="adding", total=len(todo))
            added_any = False
            for start in range(0, len(todo), BATCH_SIZE):
                batch = todo[start : start + BATCH_SIZE]
                for p in await self._add_batch(batch):
                    self._catalog.upsert(str(p), stats[p].st_mtime, stats[p].st_size)
                    if not added_any:
                        added_any = True
                        self._set_cognify_pending()
                self.status["done"] = start + len(batch)
                self._catalog.save()
            # The pending marker survives crashes and errors, so files whose
            # cognify never completed get their graph built on the next run
            # even when no new files were added.
            if added_any or self._cognify_pending():
                self.status["state"] = "cognifying"
                await self._adapter.cognify()
                self._clear_cognify_pending()
            self.status["state"] = "idle"
        except Exception as exc:  # surfaced to the app via /index/status
            traceback.print_exc()
            self.status.update(state="error", error=f"{type(exc).__name__}: {exc}")
        finally:
            self._catalog.save()

    @property
    def _pending_marker(self) -> Path:
        return self._settings.data_dir / "cognify_pending"

    def _set_cognify_pending(self) -> None:
        self._pending_marker.parent.mkdir(parents=True, exist_ok=True)
        self._pending_marker.touch()

    def _cognify_pending(self) -> bool:
        return self._pending_marker.exists()

    def _clear_cognify_pending(self) -> None:
        self._pending_marker.unlink(missing_ok=True)

    async def _add_batch(self, batch: list[Path]) -> list[Path]:
        """Add a batch, falling back to one-by-one so a single unsupported or
        unreadable file (e.g. .docx without cognee's document loader) skips
        just that file instead of sinking the whole indexing run."""
        try:
            await self._adapter.add([str(p) for p in batch])
            return batch
        except Exception:
            added = []
            for p in batch:
                try:
                    await self._adapter.add([str(p)])
                    added.append(p)
                except Exception as exc:
                    self.status["skipped"] += 1
                    self.status["last_skip"] = f"{p.name}: {exc}"
            return added
