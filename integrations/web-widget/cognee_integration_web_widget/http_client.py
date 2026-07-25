"""Thin HTTP client for a running cognee server.

Wraps the three cognee endpoints the widget needs — ``POST /api/v1/remember`` |
``/recall`` | ``/forget`` — so the adapter never imports cognee. cognee itself
(and its ``LLM_API_KEY``) is configured on the server, not here. Auth is via an
``X-Api-Key`` header when a key is configured; a local server with access
control disabled works without one.

A missing dataset (a docs corpus that was never seeded, or a conversation with
no memory yet) is reported by cognee as a 4xx; that is a normal "nothing here
yet" state, so ``recall`` returns ``[]`` and ``forget`` is a no-op for 4xx. Only
5xx / connection failures propagate, so the widget can tell "empty" apart from
"backend down".
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("web_widget.http_client")


class CogneeHttpClient:
    """Talks to a running cognee server over its HTTP API."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        *,
        client: Any = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("COGNEE_BASE_URL", "http://localhost:8000")).rstrip(
            "/"
        )
        self.api_key = api_key if api_key is not None else os.getenv("COGNEE_API_KEY", "")
        self._client = client
        self._timeout = timeout

    async def remember(
        self, text: str, *, dataset_name: str, session_id: Optional[str] = None
    ) -> None:
        """Durably store ``text`` in ``dataset_name`` (cognee add + cognify).

        When ``session_id`` is set the server also attributes the memory to that
        session; the widget uses this only for conversation-scoped writes, and
        leaves it unset when seeding the shared, read-only docs corpus.
        """
        data = {"datasetName": dataset_name}
        if session_id:
            data["session_id"] = session_id
        response = await self._request(
            "POST",
            "/api/v1/remember",
            data=data,
            files={"data": ("message.txt", text.encode("utf-8"), "text/plain")},
        )
        response.raise_for_status()

    async def recall(
        self,
        query: str,
        *,
        datasets: Optional[list[str]] = None,
        session_id: Optional[str] = None,
        top_k: int = 8,
    ) -> list[Any]:
        """Recall results for ``query``.

        ``datasets`` scopes the search to named datasets (``None`` = every dataset
        the caller can read); ``session_id`` makes the server's session-aware
        recall both use *and* persist this conversation's history. Returns the raw
        list of result objects (dicts) cognee reports. A missing dataset (4xx)
        yields ``[]``; 5xx / connection errors propagate.
        """
        response = await self._request(
            "POST",
            "/api/v1/recall",
            json={
                "query": query,
                "datasets": datasets,
                "session_id": session_id,
                "top_k": top_k,
                "include_references": True,
                "search_type": "GRAPH_COMPLETION",
            },
        )
        if response.status_code >= 500:
            response.raise_for_status()
        if response.status_code >= 400:
            return []
        data = response.json()
        results = data.get("results", data) if isinstance(data, dict) else data
        return list(results) if isinstance(results, list) else []

    async def forget(self, *, dataset_name: str) -> None:
        """Best-effort clear of ``dataset_name``.

        Forget is idempotent: a conversation whose memory only ever lived in the
        session cache has no dataset to delete, and cognee currently answers a
        forget on a never-created dataset with a 500 (an internal AttributeError)
        rather than a 404. Since there is genuinely nothing to remove in that
        case, any non-2xx is logged and swallowed rather than surfaced as a
        backend error — a real backend outage still shows up on the next recall.
        """
        try:
            response = await self._request(
                "POST", "/api/v1/forget", json={"dataset": dataset_name, "everything": False}
            )
        except Exception as error:  # noqa: BLE001 - forget is best-effort
            logger.warning("web_widget: forget transport error for %r: %s", dataset_name, error)
            return
        if response.status_code >= 400:
            logger.info(
                "web_widget: forget on %r returned %s (nothing to clear or best-effort)",
                dataset_name,
                response.status_code,
            )

    # -- transport ---------------------------------------------------------
    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key} if self.api_key else {}

    async def _request(self, method: str, path: str, **kwargs):
        import httpx

        url = self.base_url + path
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        if self._client is not None:
            return await self._client.request(method, url, headers=headers, **kwargs)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.request(method, url, headers=headers, **kwargs)
