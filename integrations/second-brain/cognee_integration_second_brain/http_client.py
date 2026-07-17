"""Thin HTTP client for a running cognee server.

Wraps the three cognee endpoints the bot needs — ``POST /api/v1/remember`` |
``/recall`` | ``/forget`` — so the adapter never imports cognee. cognee itself
(and its ``LLM_API_KEY``) is configured on the server, not here. Auth is via an
``X-Api-Key`` header when a key is configured; a local server with access
control disabled works without one.

A missing dataset (a brain with no memory yet, or one just wiped by ``/forget
me``) is reported by cognee as a 4xx; that is a normal "nothing here yet" state,
so ``recall`` returns ``[]`` and ``forget`` is a no-op for 4xx. Only 5xx /
connection failures propagate, so the bot can tell "empty" apart from "backend
down".
"""

from __future__ import annotations

import os
from typing import Any, Optional


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

        The second-brain adapter is dataset-only, so it passes no ``session_id``;
        the parameter is kept for parity with the HTTP contract.
        """
        data = {"datasetName": dataset_name}
        if session_id is not None:
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
        dataset_name: str,
        top_k: int = 15,
        session_id: Optional[str] = None,
    ) -> list[Any]:
        """Recall results for ``query`` from ``dataset_name``.

        Returns the raw list of result objects (dicts) cognee reports. A missing
        dataset (4xx) yields ``[]``; 5xx / connection errors propagate.
        """
        body: dict[str, Any] = {
            "query": query,
            "datasets": [dataset_name],
            "top_k": top_k,
            "include_references": True,
            "search_type": "GRAPH_COMPLETION",
        }
        if session_id is not None:
            body["session_id"] = session_id
        response = await self._request("POST", "/api/v1/recall", json=body)
        if response.status_code >= 500:
            response.raise_for_status()
        if response.status_code >= 400:
            return []
        data = response.json()
        results = data.get("results", data) if isinstance(data, dict) else data
        return list(results) if isinstance(results, list) else []

    async def forget(self, *, dataset_name: str) -> None:
        """Clear ``dataset_name``. A missing dataset (4xx) is a no-op."""
        response = await self._request(
            "POST", "/api/v1/forget", json={"dataset": dataset_name, "everything": False}
        )
        if response.status_code >= 500:
            response.raise_for_status()

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
