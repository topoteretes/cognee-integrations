"""A Deep Agents backend whose ``/memories/`` files also become cognee memory.

``CogneeBackend`` wraps a real backend (a ``StateBackend`` by default, or any
``BackendProtocol`` such as ``FilesystemBackend``) for the faithful filesystem
behaviour the agent sees — reads, listings, edits — and mirrors every write and
delete into cognee: a file written to ``/memories/`` is remembered into the
dataset (cross-session graph memory), a deleted file is forgotten by the data
ids the mirror recorded. Reads are never served from cognee: the wrapped
backend stays the source of truth, so the agent always gets back exactly the
bytes it wrote.

Like the tracing middleware, the mirror must never break the agent: a failed
remember or forget is logged and the filesystem operation still succeeds.
"""

from __future__ import annotations

import logging
from typing import Callable

from deepagents.backends.protocol import BackendProtocol

logger = logging.getLogger(__name__)


class CogneeBackend(BackendProtocol):
    """Mirror ``/memories/`` file writes and deletes into cognee memory.

    Args:
        dataset_name: cognee dataset mirrored files are remembered into.
        backend: The wrapped ``BackendProtocol`` holding the files. Defaults to
            a ``StateBackend``; pass ``FilesystemBackend`` for files that
            survive the process.
        redact: Optional hook applied to file content before it is remembered.
    """

    def __init__(
        self,
        dataset_name: str = "main_dataset",
        backend: BackendProtocol | None = None,
        *,
        redact: Callable[[str], str] | None = None,
    ):
        if backend is None:
            from deepagents.backends.state import StateBackend

            backend = StateBackend()
        self._backend = backend
        self.dataset_name = dataset_name
        self.redact = redact
        # file path -> the data ids the mirror stored for it; forget() needs
        # them on delete. In-memory by design: a delete whose mirror missed
        # (process restarted) leaves an extra memory, never a lost file.
        self._mirrored_ids: dict[str, list[str]] = {}

    def _redact(self, text: str) -> str:
        return self.redact(text) if self.redact else text

    def _remember_content(self, file_path: str, content: str) -> None:
        """Remember a file's content; record its data ids for the forget mirror."""
        import asyncio

        async def _remember():
            import cognee

            result = await cognee.remember(
                self._redact(content), dataset_name=self.dataset_name
            )
            return [item.get("id") for item in getattr(result, "items", []) or []]

        self._mirrored_ids[file_path] = [str(item) for item in asyncio.run(_remember()) if item]

    def _forget_content(self, file_path: str) -> None:
        """Forget the memory the mirror stored for a deleted file."""
        import asyncio

        data_ids = self._mirrored_ids.pop(file_path, [])

        async def _forget():
            import cognee

            for data_id in data_ids:
                await cognee.forget(data_id=data_id, dataset=self.dataset_name)

        asyncio.run(_forget())

    # Faithful filesystem surface: the wrapped backend is the source of truth.

    def ls(self, path: str):
        return self._backend.ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000):
        return self._backend.read(file_path, offset=offset, limit=limit)

    def glob(self, pattern: str, path: str | None = None):
        return self._backend.glob(pattern, path=path)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None):
        return self._backend.grep(pattern, path=path, glob=glob, max_count=max_count)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False):
        return self._backend.edit(file_path, old_string, new_string, replace_all=replace_all)

    def download_files(self, paths: list[str]):
        return self._backend.download_files(paths)

    def write(self, file_path: str, content: str):
        result = self._backend.write(file_path, content)
        self._mirror(file_path, content)
        return result

    def upload_files(self, files: list[tuple[str, bytes]]):
        result = self._backend.upload_files(files)
        for file_path, content in files:
            self._mirror(file_path, content.decode("utf-8", "replace"))
        return result

    def delete(self, file_path: str):
        result = self._backend.delete(file_path)
        self._mirror_delete(file_path)
        return result

    def _mirror(self, file_path: str, content: str) -> None:
        try:
            self._remember_content(file_path, content)
        except Exception as exc:  # noqa: BLE001 - memory must never break the agent
            logger.warning("cognee remember of %s failed; the file is stored anyway: %s", file_path, exc)

    def _mirror_delete(self, file_path: str) -> None:
        try:
            self._forget_content(file_path)
        except Exception as exc:  # noqa: BLE001 - memory must never break the agent
            logger.warning("cognee forget of %s failed; the file is deleted anyway: %s", file_path, exc)

    # Async delegates: async hooks call these, and the mirror joins them so a
    # running event loop is reused instead of a nested asyncio.run.

    async def als(self, path: str):
        return await self._backend.als(path)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000):
        return await self._backend.aread(file_path, offset=offset, limit=limit)

    async def aglob(self, pattern: str, path: str | None = None):
        return await self._backend.aglob(pattern, path=path)

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None):
        return await self._backend.agrep(pattern, path=path, glob=glob, max_count=max_count)

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False):
        return await self._backend.aedit(file_path, old_string, new_string, replace_all=replace_all)

    async def adownload_files(self, paths: list[str]):
        return await self._backend.adownload_files(paths)

    async def awrite(self, file_path: str, content: str):
        result = await self._backend.awrite(file_path, content)
        await self._amirror(file_path, content)
        return result

    async def aupload_files(self, files: list[tuple[str, bytes]]):
        result = await self._backend.aupload_files(files)
        for file_path, content in files:
            await self._amirror(file_path, content.decode("utf-8", "replace"))
        return result

    async def adelete(self, file_path: str):
        result = await self._backend.adelete(file_path)
        await self._amirror_delete(file_path)
        return result

    async def _amirror(self, file_path: str, content: str) -> None:
        try:
            import cognee

            result = await cognee.remember(self._redact(content), dataset_name=self.dataset_name)
            self._mirrored_ids[file_path] = [
                str(item.get("id")) for item in getattr(result, "items", []) or [] if item.get("id")
            ]
        except Exception as exc:  # noqa: BLE001 - memory must never break the agent
            logger.warning("cognee remember of %s failed; the file is stored anyway: %s", file_path, exc)

    async def _amirror_delete(self, file_path: str) -> None:
        try:
            import cognee

            for data_id in self._mirrored_ids.pop(file_path, []):
                await cognee.forget(data_id=data_id, dataset=self.dataset_name)
        except Exception as exc:  # noqa: BLE001 - memory must never break the agent
            logger.warning("cognee forget of %s failed; the file is deleted anyway: %s", file_path, exc)
