"""One contract, three cognee backends — the standardized access layer.

Integrations code against the same six calls whether cognee runs in-process,
on a server / cognee cloud, or not at all:

- ``add(paths)`` / ``cognify()`` — ingest files and build the graph
- ``chunks(q)`` / ``answer(q)`` — semantic passage search / graph answer
- ``remember(text)`` / ``recall(q)`` — the chat-memory surface (HTTP only)

Adapters:

- :class:`LocalCogneeAdapter` — cognee in-process. Pair with
  :func:`cognee_backend_core.runtime.single_user_runtime` for the fast
  single-user posture (warm persistent engine, ~0.25s searches).
- :class:`HttpCogneeAdapter` — a cognee server or cognee cloud over HTTP,
  with the error semantics the Claude Code plugin proved out: a missing
  dataset (4xx) is "no results", not an error; 5xx / connection failures
  propagate so callers can tell "empty" apart from "backend down".
- :class:`FakeAdapter` — in-memory substring search: no keys, no network,
  so any integration can be run and tested offline end to end.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from .results import _is_refusal, best_text, completions_by_dataset, unwrap_results


def _attributed_answer(raw: list[Any]) -> dict[str, Any]:
    """The best completion plus which datasets substantively contributed
    (refusal-only datasets are not sources). Shared by the local and HTTP
    adapters so attribution means the same thing in both modes."""
    pairs = completions_by_dataset(raw)
    answer = best_text([text for _, text in pairs])
    if not answer:
        return {"answer": "", "sources": []}
    # A co-source must have said something substantive, not just produced
    # a short on-topic guess — otherwise every dataset with any text
    # inflates the count and the attribution stops meaning anything.
    floor = max(150, int(0.3 * len(answer)))
    substantial = [
        name
        for name, text in pairs
        if name and not _is_refusal(text) and (text == answer or len(text) >= floor)
    ]
    # the winning dataset first, the rest in listing order
    winner = next((name for name, text in pairs if text == answer), "")
    sources = [winner] + [n for n in substantial if n != winner] if winner else substantial
    return {"answer": answer, "sources": [s for s in sources if s]}


class LocalCogneeAdapter:
    """In-process cognee. Needs ``LLM_API_KEY``; data stays on this machine.

    Feature parity with :class:`HttpCogneeAdapter` — multi-dataset search
    with per-dataset attribution, remember, exclusions — so connectors and
    handover behave the same against local cognee as against a cloud tenant.
    """

    def __init__(
        self,
        dataset: str,
        *,
        search_all: bool = False,
        exclude_datasets: Any = None,
        exclude_predicate: Any = None,
    ) -> None:
        self.dataset = dataset  # where writes land
        self.search_all = search_all
        self.exclude_datasets = set(exclude_datasets or ())
        self.exclude_predicate = exclude_predicate
        self._dataset_names: tuple[float, list[str]] = (0.0, [])

    async def add(self, paths: list[str], dataset: str = "") -> None:
        import cognee

        await cognee.add(paths, dataset_name=dataset or self.dataset)

    async def cognify(self, datasets: Any = None) -> None:
        """Build the graph in-process, sharing the single persistent engine.

        In single-user posture one warm engine owns the store for the process
        lifetime — that is what makes searches ~0.25s instead of ~4s. A
        separate cognify process would fight that engine for the store lock.
        """
        import cognee

        await cognee.cognify(datasets=list(datasets) if datasets else [self.dataset])

    async def chunks(self, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        # always scoped to this adapter's dataset, for type-ahead latency —
        # same contract as the HTTP adapter
        results = await self._search(query, "CHUNKS", top_k, scope_all=False)
        return [r if isinstance(r, dict) else {"text": str(r)} for r in results or []]

    async def answer(self, query: str, top_k: int = 8) -> str:
        return (await self.answer_with_sources(query, top_k))["answer"]

    async def answer_with_sources(
        self, query: str, top_k: int = 8, search_type: str = "GRAPH_COMPLETION"
    ) -> dict[str, Any]:
        raw = await self._search_raw(query, search_type, top_k, scope_all=self.search_all)
        return _attributed_answer(raw)

    async def remember(self, text: str, *, filename: str = "note.txt", node_set: str = "") -> None:
        """Durably store ``text``; the graph builds in the background so the
        caller (inbox ingest, feedback) is not blocked on an LLM pipeline."""
        import asyncio

        import cognee

        await cognee.add(text, dataset_name=self.dataset, node_set=[node_set] if node_set else None)

        async def _cognify_quietly() -> None:
            try:
                await cognee.cognify(datasets=[self.dataset])
            except Exception:
                pass  # the pending-marker path rebuilds on the next index run

        asyncio.get_running_loop().create_task(_cognify_quietly())

    async def recall(self, query: str, top_k: int = 15) -> list[Any]:
        return await self._search(query, "GRAPH_COMPLETION", top_k)

    async def _search(
        self, query: str, search_type: str, top_k: int, scope_all: bool | None = None
    ) -> list[Any]:
        return unwrap_results(await self._search_raw(query, search_type, top_k, scope_all))

    async def _readable_datasets(self) -> list[str]:
        """Local dataset names, cached briefly (same contract as HTTP)."""
        stamp, names = self._dataset_names
        if time.time() - stamp < 60:
            return names
        from cognee.modules.data.methods import get_datasets
        from cognee.modules.users.methods import get_default_user

        user = await get_default_user()
        names = [str(d.name) for d in await get_datasets(user.id)]
        self._dataset_names = (time.time(), names)
        return names

    async def _search_raw(
        self, query: str, search_type: str, top_k: int, scope_all: bool | None = None
    ) -> list[Any]:
        """Search, keeping per-dataset envelopes (for attribution).

        A merged local search returns flat payloads with no dataset names,
        so scope-all runs one search per dataset — exactly what the server
        does for an explicit dataset list — and wraps each result set in an
        envelope. Connector datasets (github-<repo>) are first-class this
        way: they answer, and they show up in attribution.
        """
        scope_all = self.search_all if scope_all is None else scope_all
        if not scope_all:
            names = [self.dataset]
        else:
            names = [
                n
                for n in await self._readable_datasets()
                if n not in self.exclude_datasets
                and not (self.exclude_predicate and self.exclude_predicate(n))
            ] or [self.dataset]

        from cognee import SearchType
        from cognee.api.v1.search import search

        async def one(name: str) -> dict[str, Any]:
            try:
                results = await search(
                    query_text=query,
                    query_type=SearchType[search_type],
                    datasets=[name],
                    top_k=top_k,
                )
            except Exception:
                results = []  # a missing/empty dataset is "no results"
            return {"dataset_name": name, "dataset_id": "", "search_result": list(results or [])}

        return list(await asyncio.gather(*(one(n) for n in names)))


class HttpCogneeAdapter:
    """A running cognee server (e.g. localhost:8000) or cognee cloud, over HTTP."""

    def __init__(
        self,
        dataset: str,
        base_url: str,
        api_key: str = "",
        *,
        search_all: bool = False,
        exclude_datasets: Any = None,
        exclude_predicate: Any = None,
        client: Any = None,
    ) -> None:
        self.dataset = dataset  # where writes (add/remember) land
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # Search the whole tenant (every dataset the key can read) instead of
        # only ``dataset`` — the right default when connecting to a cloud
        # tenant that already has data in other datasets.
        self.search_all = search_all
        # Datasets to keep out of tenant-wide searches (e.g. a catch-all
        # ``default_dataset`` holding unrelated material). The predicate form
        # handles name patterns unknowable up front (other users' inboxes).
        self.exclude_datasets = set(exclude_datasets or ())
        self.exclude_predicate = exclude_predicate
        self._dataset_names: tuple[float, list[str]] = (0.0, [])
        self._client = client  # injectable for tests

    # -- files / graph -------------------------------------------------------
    async def add(self, paths: list[str], dataset: str = "") -> None:
        files = []
        for p in paths:
            path = Path(p)
            files.append(("data", (path.name, path.read_bytes(), "application/octet-stream")))
        response = await self._request(
            "POST",
            "/api/v1/add",
            data={"datasetName": dataset or self.dataset},
            files=files,
        )
        response.raise_for_status()

    async def cognify(self, datasets: Any = None) -> None:
        payload = {"datasets": list(datasets) if datasets else [self.dataset]}
        response = await self._request("POST", "/api/v1/cognify", json=payload)
        response.raise_for_status()

    async def chunks(self, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        # Chunk search backs search-as-you-type: always scoped to this
        # adapter's dataset so latency stays interactive even when the tenant
        # holds large corpora (e.g. thousands of agent-session nodes).
        return await self._search(query, "CHUNKS", top_k, scope_all=False)

    async def answer(self, query: str, top_k: int = 8) -> str:
        return (await self.answer_with_sources(query, top_k))["answer"]

    async def answer_with_sources(
        self, query: str, top_k: int = 8, search_type: str = "GRAPH_COMPLETION"
    ) -> dict[str, Any]:
        # Answers are worth a wait: span everything the key can read.
        raw = await self._search_raw(query, search_type, top_k, scope_all=self.search_all)
        return _attributed_answer(raw)

    # -- chat memory (the second-brain / Claude Code plugin surface) ----------
    async def remember(self, text: str, *, filename: str = "note.txt", node_set: str = "") -> None:
        """Durably store ``text`` (server-side add + background cognify)."""
        data: dict[str, Any] = {"datasetName": self.dataset, "run_in_background": "true"}
        if node_set:
            data["node_set"] = node_set
        response = await self._request(
            "POST",
            "/api/v1/remember",
            data=data,
            files=[("data", (filename, text.encode("utf-8"), "text/plain"))],
        )
        response.raise_for_status()

    async def recall(self, query: str, top_k: int = 15) -> list[Any]:
        """Graph-grounded recall. A missing dataset (4xx) yields ``[]``."""
        body: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "search_type": "GRAPH_COMPLETION",
        }
        if not self.search_all:
            body["datasets"] = [self.dataset]
        response = await self._request("POST", "/api/v1/recall", json=body)
        if response.status_code >= 500:
            response.raise_for_status()
        if response.status_code >= 400:
            return []
        data = response.json()
        results = data.get("results", data) if isinstance(data, dict) else data
        return unwrap_results(list(results) if isinstance(results, list) else [])

    async def forget(self) -> None:
        """Clear the dataset. A missing dataset (4xx) is a no-op."""
        response = await self._request(
            "POST", "/api/v1/forget", json={"dataset": self.dataset, "everything": False}
        )
        if response.status_code >= 500:
            response.raise_for_status()

    # -- transport --------------------------------------------------------------
    async def _search(
        self, query: str, search_type: str, top_k: int, scope_all: bool | None = None
    ) -> list[Any]:
        return unwrap_results(await self._search_raw(query, search_type, top_k, scope_all))

    async def _search_raw(
        self, query: str, search_type: str, top_k: int, scope_all: bool | None = None
    ) -> list[Any]:
        """Search, keeping the per-dataset envelopes (for attribution)."""
        scope_all = self.search_all if scope_all is None else scope_all
        body: dict[str, Any] = {"query": query, "searchType": search_type, "topK": top_k}
        if not scope_all:
            body["datasets"] = [self.dataset]
        elif self.exclude_datasets or self.exclude_predicate:
            # "everything except the excluded" needs an explicit list
            names = [
                n
                for n in await self._readable_datasets()
                if n not in self.exclude_datasets
                and not (self.exclude_predicate and self.exclude_predicate(n))
            ]
            if names:
                body["datasets"] = names
        response = await self._request("POST", "/api/v1/search", json=body)
        # A dataset that does not exist yet is "no results", not an error.
        if response.status_code >= 500:
            response.raise_for_status()
        if response.status_code >= 400:
            return []
        data = response.json()
        results = data.get("results", data) if isinstance(data, dict) else data
        return list(results) if isinstance(results, list) else []

    async def _request(self, method: str, path: str, **kwargs):
        import httpx

        headers = {"X-Api-Key": self.api_key} if self.api_key else {}
        headers.update(kwargs.pop("headers", {}))
        url = self.base_url + path
        if self._client is not None:
            return await self._client.request(method, url, headers=headers, **kwargs)
        # One connection for the adapter's lifetime: recreating the client per
        # request pays TCP+TLS setup (hundreds of ms against a cloud tenant)
        # on every search. follow_redirects: cognee cloud tenants 307 routes
        # to their trailing-slash forms; httpx preserves method+body on 307/308.
        if self._own_client is None:
            self._own_client = httpx.AsyncClient(timeout=300.0, follow_redirects=True)
        return await self._own_client.request(method, url, headers=headers, **kwargs)

    async def _readable_datasets(self) -> list[str]:
        """Dataset names the key can read, cached for a minute."""
        import time

        stamp, names = self._dataset_names
        if time.time() - stamp < 60:
            return names
        response = await self._request("GET", "/api/v1/datasets")
        if response.status_code < 400:
            data = response.json()
            names = [str(d.get("name", "")) for d in data] if isinstance(data, list) else []
            self._dataset_names = (time.time(), names)
        return self._dataset_names[1]

    _own_client: Any = None

    async def aclose(self) -> None:
        if self._own_client is not None:
            await self._own_client.aclose()
            self._own_client = None

    # context-manager support, so short-lived uses can't leak the pooled
    # connections: `async with HttpCogneeAdapter(...) as adapter: ...`
    async def __aenter__(self) -> "HttpCogneeAdapter":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()


class FakeAdapter:
    """Offline stand-in: reads the files it is given and does substring search."""

    def __init__(self, dataset: str = "main") -> None:
        self.dataset = dataset
        self._docs: dict[str, str] = {}  # path -> text
        self.dataset_of: dict[str, str] = {}  # path -> dataset it landed in
        self.cognified = False
        self.cognified_datasets: list[str] = []

    async def add(self, paths: list[str], dataset: str = "") -> None:
        for p in paths:
            try:
                self._docs[p] = Path(p).read_text(errors="ignore")
            except OSError:
                self._docs[p] = ""
            self.dataset_of[p] = dataset or self.dataset

    async def cognify(self, datasets: Any = None) -> None:
        await asyncio.sleep(0)  # keep the same async shape as the real thing
        self.cognified = True
        self.cognified_datasets = list(datasets) if datasets else [self.dataset]

    async def chunks(self, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        q = query.lower()
        hits = []
        for path, text in self._docs.items():
            idx = text.lower().find(q)
            if idx == -1:
                continue
            start = max(0, idx - 80)
            snippet = text[start : idx + len(q) + 120].strip()
            hits.append({"text": snippet, "file_path": path, "name": Path(path).name})
            if len(hits) >= top_k:
                break
        return hits

    async def answer(self, query: str, top_k: int = 8) -> str:
        hits = await self.chunks(query, top_k=1)
        if not hits:
            # whole phrase missed; fall back to the doc matching the most words
            words = [w for w in query.lower().split() if len(w) > 3]
            best, best_count = None, 0
            for path, text in self._docs.items():
                count = sum(w in text.lower() for w in words)
                if count > best_count:
                    best, best_count = path, count
            if best is None:
                return ""
            hits = [{"name": Path(best).name, "text": _trim_line(self._docs[best])}]
        return f"(fake mode) Closest passage from {hits[0]['name']}: {hits[0]['text']}"


def _trim_line(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
