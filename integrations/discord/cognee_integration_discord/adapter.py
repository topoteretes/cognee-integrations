"""Chat-memory adapter seam.

``ChatMemoryAdapter`` is the thin interface every platform bot (Discord, Slack,
Telegram) talks to, so the bot layer never calls cognee directly. This mirrors
the shared adapter proposed in #3608 — when that lands, its implementation drops
in here and the bot/service layers are untouched.

This branch ships ``CogneeHttpAdapter``, which talks to a running cognee server
over HTTP (``/api/v1/remember`` | ``/recall`` | ``/forget``). A sibling branch
provides an in-process SDK implementation instead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class RecallResult:
    """Normalized recall payload the bot layer can render without knowing cognee."""

    answer: str
    sources: list[str] = field(default_factory=list)


class ChatMemoryAdapter(ABC):
    """Minimal memory surface a chat bot needs: remember, recall, forget."""

    @abstractmethod
    async def remember(
        self, text: str, *, dataset: str, session: str, provenance: Optional[dict] = None
    ) -> None:
        """Persist a message into memory under the given dataset/session."""

    @abstractmethod
    async def recall(
        self, query: str, *, dataset: str, session: str, top_k: int = 5
    ) -> RecallResult:
        """Answer a question from the dataset/session memory."""

    @abstractmethod
    async def forget(self, *, dataset: str, everything: bool = False) -> None:
        """Remove a dataset's memory (or everything the bot owns)."""


class CogneeHttpAdapter(ChatMemoryAdapter):
    """Adapter that talks to a running cognee server over HTTP.

    Auth is via ``X-Api-Key`` when an api key is configured; a local cognee with
    access control disabled works without one. An ``httpx.AsyncClient`` can be
    injected (tests); otherwise one is created per request.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        *,
        client: Any = None,
        timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._client = client

    async def remember(
        self, text: str, *, dataset: str, session: str, provenance: Optional[dict] = None
    ) -> None:
        response = await self._request(
            "POST",
            "/api/v1/remember",
            data={"datasetName": dataset, "session_id": session},
            files={"data": ("message.txt", text.encode("utf-8"), "text/plain")},
        )
        response.raise_for_status()

    async def recall(
        self, query: str, *, dataset: str, session: str, top_k: int = 5
    ) -> RecallResult:
        response = await self._request(
            "POST",
            "/api/v1/recall",
            json={
                "query": query,
                "top_k": top_k,
                "session_id": session,
                "datasets": [dataset],
                "include_references": True,
            },
        )
        response.raise_for_status()
        return _parse_recall(response.json())

    async def forget(self, *, dataset: str, everything: bool = False) -> None:
        response = await self._request(
            "POST",
            "/api/v1/forget",
            json={"dataset": dataset, "everything": everything},
        )
        response.raise_for_status()

    # -- transport ------------------------------------------------------------

    def _headers(self) -> dict:
        return {"X-Api-Key": self._api_key} if self._api_key else {}

    async def _request(self, method: str, path: str, **kwargs):
        import httpx

        url = self._base_url + path
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        if self._client is not None:
            return await self._client.request(method, url, headers=headers, **kwargs)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.request(method, url, headers=headers, **kwargs)


def _parse_recall(data: Any) -> RecallResult:
    """Normalize cognee's recall response (list of results / error) to RecallResult."""
    items = data.get("results", data) if isinstance(data, dict) else data
    texts: list[str] = []
    if isinstance(items, list):
        for item in items:
            text = _item_text(item)
            if text:
                texts.append(text)
    return RecallResult(answer=texts[0] if texts else "", sources=texts)


def _item_text(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        text = item.get("text")
        if not text:
            search_result = item.get("search_result")
            if isinstance(search_result, list):
                text = "\n".join(str(part) for part in search_result)
            elif isinstance(search_result, str):
                text = search_result
        if isinstance(text, str):
            return text.strip()
    return ""
