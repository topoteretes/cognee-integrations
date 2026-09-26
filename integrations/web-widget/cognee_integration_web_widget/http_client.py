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


def normalise_run_status(value) -> str:
    """One vocabulary for a pipeline state, whichever endpoint reported it.

    ``/status`` answers DATASET_PROCESSING_COMPLETED where ``/status/progress``
    answers "completed", and a page should not have to know which it is talking
    to. Completed is tested before running because the long form contains both
    words.
    """
    text = str(value or "").lower()
    if not text:
        return "unknown"
    if "completed" in text or "finished" in text:
        return "completed"
    if "error" in text or "fail" in text:
        return "failed"
    if "processing" in text or "running" in text or "started" in text:
        return "running"
    if "pending" in text or "queued" in text or "waiting" in text:
        return "pending"
    return "unknown"


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

    # -- read-only introspection (dashboard) -------------------------------

    async def list_datasets(self) -> list[Any]:
        """Every dataset this key can read. ``[]`` rather than raising on 4xx."""
        response = await self._request("GET", "/api/v1/datasets/")
        if response.status_code >= 400:
            return []
        data = response.json()
        items = data.get("datasets", data) if isinstance(data, dict) else data
        return list(items) if isinstance(items, list) else []

    async def dataset_data(self, dataset_id: str) -> list[Any]:
        """The items ingested into one dataset."""
        response = await self._request("GET", f"/api/v1/datasets/{dataset_id}/data")
        if response.status_code >= 400:
            return []
        data = response.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        return list(items) if isinstance(items, list) else []

    async def recall_history(self) -> list[Any]:
        """Questions this key's principal has asked, newest-first per the server.

        cognee records every recall it answers, so this is the widget's question
        log without the widget storing anything itself.
        """
        response = await self._request("GET", "/api/v1/recall")
        if response.status_code >= 400:
            return []
        data = response.json()
        items = data.get("results", data) if isinstance(data, dict) else data
        return list(items) if isinstance(items, list) else []

    async def delete_data(self, *, dataset_id: str, data_id: str) -> bool:
        """Permanently remove one ingested item. True when the server accepted it."""
        response = await self._request("DELETE", f"/api/v1/datasets/{dataset_id}/data/{data_id}")
        return response.status_code < 400

    async def fetch_raw(self, *, dataset_id: str, data_id: str) -> Optional[bytes]:
        """The bytes originally ingested for one item, or None if unavailable."""
        response = await self._request("GET", f"/api/v1/datasets/{dataset_id}/data/{data_id}/raw")
        if response.status_code >= 400:
            return None
        return response.content

    async def remember_bytes(
        self, content: bytes, *, dataset_name: str, filename: str, content_type: str
    ) -> None:
        """Store raw bytes under ``filename``, preserving the original type.

        ``remember`` encodes a str as message.txt; re-ingesting an existing item
        has to keep its own name, or the corpus fills with items called
        "message" and the source becomes unidentifiable in the dashboard.
        """
        response = await self._request(
            "POST",
            "/api/v1/remember",
            data={"datasetName": dataset_name},
            files={"data": (filename, content, content_type)},
        )
        response.raise_for_status()

    async def graph(self, dataset_id: str) -> dict:
        """The whole knowledge graph. Megabytes - never fetch this on page load."""
        response = await self._request("GET", f"/api/v1/datasets/{dataset_id}/graph")
        if response.status_code >= 400:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {}

    async def visualize_html(self, dataset_id: str) -> Optional[str]:
        """cognee's own rendered graph page - the same artifact the SDK writes.

        Slow enough that the caller must cache it; see the dashboard route.
        """
        response = await self._request(
            "GET",
            "/api/v1/visualize",
            params={"dataset_id": dataset_id},
            timeout_override=180.0,
        )
        if response.status_code >= 400:
            return None
        return response.text

    async def forget_dataset(self, dataset_name: str) -> bool:
        """Delete an entire dataset and everything in it."""
        response = await self._request(
            "POST", "/api/v1/forget", json={"dataset": dataset_name, "everything": False}
        )
        return response.status_code < 400

    async def remember_background(
        self,
        content: bytes,
        *,
        dataset_name: str,
        filename: str,
        node_set: Optional[list[str]] = None,
    ) -> bool:
        """Queue one document for ingest without waiting for its graph build.

        A bulk ingest is hundreds of these, and each one cognifies. Waiting
        would hold the request open for the whole run, so the server is asked to
        process in the background and the upload returns as soon as it is
        accepted.

        ``node_set`` tags the nodes this document produces. cognee takes the
        field repeated, once per tag, and ``recall``'s ``node_name`` filters on
        the same values, so a tag written here is what makes part of a corpus
        retrievable on its own later.
        """
        data: dict = {"datasetName": dataset_name, "run_in_background": "true"}
        if node_set:
            data["node_set"] = list(node_set)
        response = await self._request(
            "POST",
            "/api/v1/remember",
            data=data,
            files={"data": (filename, content, "text/plain")},
            timeout_override=120.0,
        )
        return response.status_code < 400

    async def remember_repo(self, repo_url: str, *, dataset_name: str) -> tuple[bool, str]:
        """Index a git repository as a code graph.

        A different route through the same endpoint: ``content_type='code'``
        takes repository specs in ``raw_data`` and refuses file uploads, because
        cognee clones the repository and walks it itself rather than being
        handed bytes. That is why the browser-reads-the-files arrangement the
        rest of ingest uses cannot reach this - a code graph needs the
        repository, not its contents.

        No node_set is sent. The field is accepted by the endpoint and dropped
        on this path - a repository indexed with one produces no NodeSet node
        and nothing recall can filter on - so sending it would only promise a
        tag that does not exist. Code nodes carry ``repo`` instead.

        Returns whether it was accepted and what cognee said if it was not, so
        the page can show the reason rather than a status code.
        """
        data: dict = {
            "datasetName": dataset_name,
            "run_in_background": "true",
            "content_type": "code",
            "raw_data": [repo_url],
        }
        response = await self._request(
            "POST", "/api/v1/remember", data=data, timeout_override=120.0
        )
        if response.status_code < 400:
            return True, ""
        try:
            detail = str(response.json().get("detail") or "")[:300]
        except ValueError:
            detail = response.text[:300]
        return False, detail or f"cognee answered {response.status_code}"

    async def dataset_progress(self, dataset_id: str, pipeline: str = "cognify_pipeline") -> dict:
        """How far cognee has got building the graph for ``dataset_id``.

        ``remember`` with ``run_in_background`` returns once the upload is
        accepted, so this is the only way to see the work that follows it.

        Two endpoints, because not every cognee has both. ``/status/progress``
        carries item counts and a stage; where it is missing - it 404s on the
        deployment this was built against - ``/status`` still answers with the
        state alone. Which one answered is reported rather than hidden: a
        deployment that cannot count items and a run that has not started are
        different things, and collapsing them into one empty answer made the
        page wait for numbers that were never coming.
        """
        response = await self._request(
            "GET",
            "/api/v1/datasets/status/progress",
            params={"dataset": dataset_id, "pipeline": pipeline},
        )
        if response.status_code == 404:
            return await self._dataset_status(dataset_id, pipeline)
        if response.status_code >= 400:
            return {"status": "", "progress": None, "counts_available": False}
        body = response.json()
        entry = (body.get(dataset_id) or {}) if isinstance(body, dict) else {}
        return {
            "status": normalise_run_status(entry.get("status")),
            "progress": entry.get("progress") or None,
            "counts_available": True,
        }

    async def _dataset_status(self, dataset_id: str, pipeline: str) -> dict:
        """The state of a run, with no item counts, from the older endpoint."""
        response = await self._request(
            "GET",
            "/api/v1/datasets/status",
            params={"dataset": dataset_id, "pipeline": pipeline},
        )
        if response.status_code >= 400:
            return {"status": "", "progress": None, "counts_available": False}
        body = response.json()
        value = body.get(dataset_id) if isinstance(body, dict) else None
        if isinstance(value, dict):
            value = value.get(pipeline)
        return {
            "status": normalise_run_status(value),
            "progress": None,
            "counts_available": False,
        }

    async def list_sessions(self) -> list[Any]:
        """Every session this key can see, widget conversations among them."""
        response = await self._request("GET", "/api/v1/sessions")
        if response.status_code >= 400:
            return []
        data = response.json()
        items = data.get("sessions", data.get("results", data)) if isinstance(data, dict) else data
        return list(items) if isinstance(items, list) else []

    async def session_detail(self, session_id: str) -> dict:
        """One session, including its ``qas`` — both sides of each exchange."""
        from urllib.parse import quote

        response = await self._request("GET", f"/api/v1/sessions/{quote(session_id, safe='')}")
        if response.status_code >= 400:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {}

    # -- transport ---------------------------------------------------------
    def _headers(self) -> dict:
        return {"X-Api-Key": self.api_key} if self.api_key else {}

    async def _request(self, method: str, path: str, **kwargs):
        import httpx

        timeout = kwargs.pop("timeout_override", None) or self._timeout

        url = self.base_url + path
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        if self._client is not None:
            return await self._client.request(method, url, headers=headers, **kwargs)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(method, url, headers=headers, **kwargs)
