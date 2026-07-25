"""Thin HTTP client for a running cognee server.

Wraps the four cognee endpoints the Slack bot needs — ``POST /api/v1/add`` |
``/cognify`` | ``/search`` | ``/forget`` — so the memory adapter never imports
cognee. Ingestion (``add``) and graph-building (``cognify``) are separate calls
so the bot can batch: many cheap adds, one cognify per batch.

cognee itself (and its ``LLM_API_KEY``) is configured on the server. Auth is via
an ``X-Api-Key`` header when a key is configured; a local server with access
control disabled works without one. A missing dataset (a channel with no memory
yet) is a 4xx cognee reports, which ``search`` maps to ``[]`` and ``forget``
treats as a no-op; only 5xx / connection failures propagate.
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
        timeout: float = 120.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("COGNEE_BASE_URL", "http://localhost:8000")).rstrip(
            "/"
        )
        self.api_key = api_key if api_key is not None else os.getenv("COGNEE_API_KEY", "")
        self._client = client
        self._timeout = timeout

    async def add(
        self, text: str, *, dataset_name: str, node_set: Optional[list[str]] = None
    ) -> None:
        """Add one message to a dataset (does not build the graph — see ``cognify``)."""
        data: dict[str, Any] = {"datasetName": dataset_name}
        if node_set:
            data["node_set"] = node_set
        response = await self._request(
            "POST",
            "/api/v1/add",
            data=data,
            files={"data": ("message.txt", text.encode("utf-8"), "text/plain")},
        )
        response.raise_for_status()

    async def cognify(self, *, dataset_name: str) -> None:
        """Build the knowledge graph for a dataset (the batched, expensive step)."""
        response = await self._request("POST", "/api/v1/cognify", json={"datasets": [dataset_name]})
        response.raise_for_status()

    async def search(
        self,
        query: str,
        *,
        search_type: str,
        dataset_name: str,
        node_name: Optional[list[str]] = None,
        top_k: int = 10,
    ) -> list[Any]:
        """Run a search; returns the raw result list. Missing dataset (4xx) → ``[]``."""
        body: dict[str, Any] = {
            "query": query,
            "search_type": search_type,
            "datasets": [dataset_name],
            "top_k": top_k,
        }
        if node_name is not None:
            body["node_name"] = node_name
        response = await self._request("POST", "/api/v1/search", json=body)
        if response.status_code >= 500:
            response.raise_for_status()
        if response.status_code >= 400:
            return []
        data = response.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("results", "search_results", "search_result"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return []

    async def forget(self, *, dataset_name: str) -> None:
        """Delete a dataset. A missing dataset (4xx) is a no-op."""
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
