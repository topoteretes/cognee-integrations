"""The memory backend: the seam between the adapter and cognee.

The adapter never calls cognee directly. It talks to a :class:`MemoryBackend`,
a four-method interface expressed entirely in the plain value objects from
:mod:`.models`. This gives two things:

* **Deterministic tests with no keys.** The suite runs the real adapter against
  an in-memory fake backend, so ``ingest`` / ``answer`` / ``forget`` round-trips
  are exercised without an LLM, embeddings, or a database.
* **Pluggable transport.** :class:`CogneeHttpMemoryBackend` (the default) talks
  to a running cognee server over its HTTP API, so a bot needs no in-process
  cognee. :class:`CogneeMemoryBackend` is an optional in-process Python-SDK
  implementation (install this package's ``[sdk]`` extra); both satisfy the same
  contract without touching the adapter or any bot.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional, Protocol, runtime_checkable
from uuid import NAMESPACE_URL, UUID, uuid5

from .models import Citation

logger = logging.getLogger("chat_memory.backend")


@runtime_checkable
class MemoryBackend(Protocol):
    """What the adapter needs from a memory system. Four methods, no cognee types."""

    async def remember(
        self,
        text: str,
        *,
        dataset: str,
        session: str,
        external_metadata: dict[str, Any],
        item_id: Optional[str] = None,
    ) -> None:
        """Store ``text`` durably in ``dataset``, returning fast.

        ``external_metadata`` is stamped onto the stored item; it carries the
        author and permalink that later power both per-user forget and
        citations. ``item_id`` is a stable, caller-chosen id for idempotency.
        ``session`` identifies the live conversation; a backend may use it for a
        recency cache (the in-memory one does) or ignore it.
        """
        ...

    async def recall(self, query: str, *, dataset: str, session: str, top_k: int) -> list[Citation]:
        """Recall the most relevant items for ``query`` from ``dataset``."""
        ...

    async def forget_scope(self, *, dataset: str) -> dict:
        """Wipe an entire ``dataset`` (the whole-scope 'forget everything here')."""
        ...

    async def forget_user(self, *, dataset: str, user: str) -> dict:
        """Forget one user's items inside a shared ``dataset`` (their 'forget me')."""
        ...


def deterministic_item_id(*parts: str) -> str:
    """A stable UUIDv5 string from key parts (e.g. channel, user, timestamp).

    Re-ingesting the same message yields the same id, so the backend can dedup
    instead of storing duplicates (captures often replay on reconnect).
    """
    return str(uuid5(NAMESPACE_URL, "|".join(parts)))


# A tiny stopword set so the lexical ranker keys off content words, not
# grammar. Keeps the in-memory demo/tests from matching on "the"/"on"/etc.
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the to "
    "was were will with we you your my our i do does when what who where how".split()
)


def _content_tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"\b\w+\b", text.lower()) if len(w) >= 2 and w not in _STOPWORDS}


def _keyword_overlap(query: str, text: str) -> int:
    """Count shared content-word tokens. A tiny deterministic ranker."""
    return len(_content_tokens(query) & _content_tokens(text))


class InMemoryMemoryBackend:
    """A dependency-free :class:`MemoryBackend` for local dev, demos, and tests.

    Keeps everything in process dictionaries and ranks recall by keyword
    overlap, so a bot (and this package's test suite) can exercise the full
    ``ingest`` -> ``answer`` -> ``forget`` contract with no LLM, embeddings,
    database, or API keys. It faithfully models the two behaviours the adapter
    depends on: dedup by ``item_id`` and per-user forget by the ``user`` field
    stamped into ``external_metadata``.

    It is intentionally not a knowledge graph: recall is lexical, so swap in
    :class:`CogneeHttpMemoryBackend` (or :class:`CogneeMemoryBackend`) for real
    multi-hop memory.
    """

    def __init__(self) -> None:
        # dataset -> item_id -> stored record
        self._store: dict[str, dict[str, dict[str, Any]]] = {}

    async def remember(
        self,
        text: str,
        *,
        dataset: str,
        session: str,
        external_metadata: dict[str, Any],
        item_id: Optional[str] = None,
    ) -> None:
        key = item_id or deterministic_item_id(dataset, text)
        # Dedup: a replayed message with the same id overwrites, never duplicates.
        self._store.setdefault(dataset, {})[key] = {
            "text": text,
            "session": session,
            "external_metadata": dict(external_metadata),
        }

    async def recall(self, query: str, *, dataset: str, session: str, top_k: int) -> list[Citation]:
        records = self._store.get(dataset, {})
        scored: list[tuple[int, Citation]] = []
        for record in records.values():
            hits = _keyword_overlap(query, record["text"])
            if hits == 0:
                continue
            stamp = record["external_metadata"]
            scored.append(
                (
                    hits,
                    Citation(
                        text=record["text"],
                        # Same-session items came from the fast cache; others
                        # are recalled from the shared dataset graph.
                        source="session" if record["session"] == session else "graph",
                        score=float(hits),
                        permalink=stamp.get("permalink"),
                        user=str(stamp["user"]) if stamp.get("user") is not None else None,
                    ),
                )
            )
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:top_k]]

    async def forget_scope(self, *, dataset: str) -> dict:
        removed = len(self._store.pop(dataset, {}))
        return {"dataset": dataset, "items_removed": removed, "status": "success"}

    async def forget_user(self, *, dataset: str, user: str) -> dict:
        records = self._store.get(dataset, {})
        to_remove = [
            key
            for key, record in records.items()
            if str(record["external_metadata"].get("user")) == str(user)
        ]
        for key in to_remove:
            del records[key]
        return {
            "dataset": dataset,
            "user": user,
            "items_removed": len(to_remove),
            "status": "success",
        }


# ---------------------------------------------------------------------------
# Provenance round-trip for the HTTP backend
#
# cognee's HTTP /remember stores opaque text and its /recall returns text (a
# synthesized answer plus, with include_references, an Evidence block of source
# snippets). There is no structured external_metadata channel over HTTP, so the
# author + permalink ride inside the stored text as a compact header line and are
# parsed back out of the recalled snippets. This mirrors the shipped Discord bot.
# ---------------------------------------------------------------------------
_PROVENANCE_PREFIX = "[cognee-chat-memory]"
_PROVENANCE_RE = re.compile(
    r"\[cognee-chat-memory\][ \t]*(?:user=(?P<user>\S+))?"
    r"[ \t]*(?:permalink=(?P<permalink>\S+))?[^\n]*\n?"
)
_EVIDENCE_MARKER = "\n\nEvidence:"


def _encode_provenance(text: str, external_metadata: dict[str, Any]) -> str:
    """Prefix ``text`` with a parseable provenance header, when there is one.

    Only ``user`` / ``permalink`` are carried (the two citation fields); a
    message with neither is stored unchanged.
    """
    user = external_metadata.get("user")
    permalink = external_metadata.get("permalink")
    parts = []
    if user is not None:
        parts.append(f"user={user}")
    if permalink is not None:
        parts.append(f"permalink={permalink}")
    if not parts:
        return text
    return f"{_PROVENANCE_PREFIX} {' '.join(parts)}\n{text}"


def _decode_provenance(text: str) -> tuple[str, Optional[str], Optional[str]]:
    """Split a stored/recalled snippet into (clean_text, user, permalink)."""
    match = _PROVENANCE_RE.search(text or "")
    if not match:
        return (text or "").strip(), None, None
    clean = (text[: match.start()] + text[match.end() :]).strip()
    return clean, match.group("user"), match.group("permalink")


def _find_provenance(text: str) -> list[tuple[Optional[str], Optional[str]]]:
    """Every provenance header found anywhere in a recall response body."""
    return [(m.group("user"), m.group("permalink")) for m in _PROVENANCE_RE.finditer(text or "")]


def _strip_evidence(text: str) -> str:
    """Drop the trailing ``Evidence:`` block cognee appends to a completion."""
    index = text.find(_EVIDENCE_MARKER)
    return text[:index] if index != -1 else text


def _item_text(item: Any) -> str:
    """Best-effort text of one recall result (a str, or a dict shaped a few ways)."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("text", "answer", "content"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
        search_result = item.get("search_result")
        if isinstance(search_result, list):
            return "\n".join(str(part) for part in search_result)
        if isinstance(search_result, str):
            return search_result
    return ""


class CogneeHttpMemoryBackend:
    """Default backend: a running cognee server over its HTTP API.

    Maps the adapter's four methods onto ``POST /api/v1/remember``,
    ``/recall``, and ``/forget``. cognee itself (and its ``LLM_API_KEY``) is
    configured on the server, not here. Auth is via an ``X-Api-Key`` header when
    an api key is configured; a local server with access control disabled works
    without one.

    Args:
        base_url: cognee server base URL. Defaults to ``COGNEE_BASE_URL`` in the
            environment, else ``http://localhost:8000``.
        api_key: cognee api key. Defaults to ``COGNEE_API_KEY`` (omit for a local
            server with access control disabled).
        client: an injected ``httpx.AsyncClient``-like object (used by tests);
            when ``None`` a client is created per request.
        timeout: per-request timeout in seconds.
        search_type: recall strategy passed to cognee (default ``GRAPH_COMPLETION``).
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        *,
        client: Any = None,
        timeout: float = 60.0,
        search_type: str = "GRAPH_COMPLETION",
    ) -> None:
        self._base_url = (base_url or os.getenv("COGNEE_BASE_URL", "http://localhost:8000")).rstrip(
            "/"
        )
        self._api_key = api_key if api_key is not None else os.getenv("COGNEE_API_KEY", "")
        self._client = client
        self._timeout = timeout
        self._search_type = search_type

    async def remember(
        self,
        text: str,
        *,
        dataset: str,
        session: str,
        external_metadata: dict[str, Any],
        item_id: Optional[str] = None,
    ) -> None:
        payload_text = _encode_provenance(text, external_metadata)
        # Durable ingest: datasetName only, NO session_id. Passing session_id routes
        # cognee into its session-cache path (which does not build a recallable,
        # user-owned dataset), so a later recall finds nothing. The dataset is the
        # durable memory; ``session`` is a recency axis the HTTP backend does not use.
        response = await self._request(
            "POST",
            "/api/v1/remember",
            data={"datasetName": dataset},
            files={"data": ("message.txt", payload_text.encode("utf-8"), "text/plain")},
        )
        response.raise_for_status()

    async def recall(self, query: str, *, dataset: str, session: str, top_k: int) -> list[Citation]:
        # Dataset-scoped graph recall (session_id intentionally omitted — see
        # remember: this backend's memory boundary is the durable dataset).
        response = await self._request(
            "POST",
            "/api/v1/recall",
            json={
                "query": query,
                "datasets": [dataset],
                "top_k": top_k,
                "include_references": True,
                "search_type": self._search_type,
            },
        )
        # A dataset that doesn't exist yet (e.g. right after a forget) is a 4xx —
        # "nothing here yet", not an error; only a failing backend (5xx) propagates.
        if response.status_code >= 500:
            response.raise_for_status()
        if response.status_code >= 400:
            return []
        return self._citations(response.json())

    async def forget_scope(self, *, dataset: str) -> dict:
        response = await self._request(
            "POST", "/api/v1/forget", json={"dataset": dataset, "everything": False}
        )
        # Forgetting a never-created dataset (4xx) is an idempotent no-op.
        if response.status_code >= 500:
            response.raise_for_status()
        return self._forget_result(response, dataset)

    async def forget_user(self, *, dataset: str, user: str) -> dict:
        """Forget a user's memory.

        cognee's HTTP surface exposes only whole-dataset forget, so this wipes
        ``dataset``. Pair per-user "forget me" with ``per_user_scope`` (a dataset
        per user) so this removes only that user's brain; precise per-user forget
        inside a *shared* dataset needs :class:`CogneeMemoryBackend` (SDK).
        """
        logger.warning(
            "chat_memory: HTTP backend forgets the whole dataset %r for user=%r "
            "(no metadata-filtered delete over HTTP). Use per_user_scope so the "
            "dataset is already per-user, or CogneeMemoryBackend for precise "
            "per-user forget in a shared dataset.",
            dataset,
            user,
        )
        result = await self.forget_scope(dataset=dataset)
        result["user"] = user
        return result

    # -- citation assembly -------------------------------------------------
    @staticmethod
    def _citations(payload: Any) -> list[Citation]:
        """Turn a cognee recall response into citations, resolving provenance.

        The first result is the synthesized answer (its ``Evidence:`` block and
        any provenance header stripped); every provenance header echoed in the
        response becomes a source citation carrying the original author/permalink.
        """
        items = payload.get("results", payload) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return []
        texts = [text for text in (_item_text(item) for item in items) if text]
        if not texts:
            return []

        citations: list[Citation] = []
        answer_clean, _, _ = _decode_provenance(_strip_evidence(texts[0]))
        if answer_clean:
            citations.append(Citation(text=answer_clean, source="graph"))

        seen: set[str] = set()
        for user, permalink in _find_provenance("\n".join(texts)):
            if not user and not permalink:
                continue
            key = permalink or user or ""
            if key in seen:
                continue
            seen.add(key)
            citations.append(
                Citation(text="", source="graph_context", user=user, permalink=permalink)
            )
        return citations

    @staticmethod
    def _forget_result(response: Any, dataset: str) -> dict:
        try:
            body = response.json()
        except Exception:  # pragma: no cover - non-JSON forget response
            body = {}
        result = {"dataset": dataset, "status": "success"}
        if isinstance(body, dict):
            result.update(body)
        return result

    # -- transport ---------------------------------------------------------
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


class CogneeMemoryBackend:
    """Optional in-process backend backed by the cognee Python SDK.

    Requires this package's ``[sdk]`` extra (``pip install
    cognee-integration-chat-memory[sdk]``). Maps the adapter's four methods onto
    ``cognee.remember`` / ``cognee.recall`` / ``cognee.forget`` and nothing else.
    Those entrypoints are imported lazily inside each method, so the SDK's
    operational import graph is only pulled in when a primitive actually runs.

    Storage is durable: a message is ingested through cognee's permanent
    ``add()`` + ``cognify()`` path (``run_in_background=True`` keeps it
    fire-and-forget). That path is the only one that writes ``external_metadata``
    onto the ``Data`` row and honours a caller-set ``data_id`` — which is exactly
    what per-user "forget me" and citation resolution rely on. cognee's
    ``session_id`` path is a session-cache-only fast path that drops both, so
    this backend does not use it; a session recency cache is a future add-on.

    Args:
        run_in_background: Pass-through to ``remember``. ``True`` (default) makes
            ingestion fire-and-forget: the call returns immediately and the
            add/cognify build proceeds in the background.
        top_k: Default recall breadth when the adapter does not override it.
    """

    def __init__(self, *, run_in_background: bool = True, top_k: int = 10) -> None:
        self.run_in_background = run_in_background
        self.top_k = top_k

    async def remember(
        self,
        text: str,
        *,
        dataset: str,
        session: str,
        external_metadata: dict[str, Any],
        item_id: Optional[str] = None,
    ) -> None:
        import cognee
        from cognee.tasks.ingestion.data_item import DataItem

        item = DataItem(
            data=text,
            external_metadata=external_metadata,
            data_id=self._as_uuid(item_id),
        )
        # Durable path (no session_id): add() writes external_metadata + honours
        # data_id onto the Data row and cognify builds the graph.
        await cognee.remember(
            item,
            dataset_name=dataset,
            run_in_background=self.run_in_background,
        )

    async def recall(self, query: str, *, dataset: str, session: str, top_k: int) -> list[Citation]:
        """Recall from the dataset graph, resolving citations to their source.

        A hit's permalink/author resolve only when the result carries a
        ``data_id`` in its provenance (chunk/context-grounded results); a
        synthesized graph answer has none, so its citation is text-only.
        Building the ``data_id -> stamp`` map reads the dataset's ``Data`` rows
        once per call.
        """
        import cognee

        responses = await cognee.recall(
            query,
            datasets=[dataset],
            top_k=top_k,
            include_references=True,
        )
        if not responses:
            return []
        # recall reports each hit's origin as a data_id (the ingested Data.id);
        # the permalink/author live in the stamp we wrote onto that Data row.
        stamps = await self._stamps_by_data_id(dataset)
        return [self._to_citation(response, stamps) for response in responses]

    async def forget_scope(self, *, dataset: str) -> dict:
        import cognee

        return await cognee.forget(dataset=dataset)

    async def forget_user(self, *, dataset: str, user: str) -> dict:
        """Delete only ``user``'s items from a shared ``dataset``.

        Resolves the user's ``Data`` rows by the ``user`` field stamped into
        ``external_metadata`` at ingest, then forgets them one by one.

        Scope note (agreed on the tracking issue): this removes the user's own
        items. When cognify has merged facts from several users into one shared
        graph node, dropping those items can leave the shared node partially
        referenced. Fully dedup-aware deletion, which removes a node or edge
        only when no other user's data still references it (the
        ``get_unique_nodes_for_data`` / ``get_unique_edges_for_data`` approach
        from cognee-rs #36), belongs in the core and is the planned follow-up.
        Per-user datasets (e.g. the second brain's ``brain:{user}``) are not
        affected: their "forget me" is a whole-dataset wipe with nothing shared
        to orphan.
        """
        import cognee

        dataset_id, rows = await self._dataset_rows(dataset, permission="delete")
        data_ids = [
            row.id
            for row in rows
            if isinstance(getattr(row, "external_metadata", None), dict)
            and str(row.external_metadata.get("user")) == str(user)
        ]
        if not data_ids:
            return {"dataset": dataset, "user": user, "items_removed": 0, "status": "success"}

        removed = 0
        for data_id in data_ids:
            try:
                # Full delete (not memory_only): "forget me" must drop the raw
                # Data record too, not just its graph/vector projection.
                await cognee.forget(data_id=data_id, dataset_id=dataset_id)
                removed += 1
            except Exception as exc:  # pragma: no cover - defensive per-item guard
                logger.warning(
                    "chat_memory: forget_user failed for data_id=%s in dataset=%s: %s",
                    data_id,
                    dataset,
                    exc,
                )
        return {
            "dataset": dataset,
            "user": user,
            "items_removed": removed,
            "status": "success",
        }

    async def _dataset_rows(
        self, dataset: str, *, permission: str = "read"
    ) -> tuple[Optional[UUID], list[Any]]:
        """Resolve ``(dataset_id, Data rows)`` for ``dataset``; ``(None, [])`` if absent.

        Shared by forget-me and citation resolution: both need the ``Data`` rows
        that carry the ``external_metadata`` stamp written at ingest. Citation
        resolution only reads (``permission="read"``); forget-me needs
        ``"delete"``.
        """
        import cognee
        from cognee.modules.data.methods.get_authorized_dataset_by_name import (
            get_authorized_dataset_by_name,
        )
        from cognee.modules.users.methods import get_default_user

        default_user = await get_default_user()
        try:
            resolved = await get_authorized_dataset_by_name(dataset, default_user, permission)
        except Exception:
            resolved = None
        if resolved is None:
            return None, []
        rows = await cognee.datasets.list_data(resolved.id, user=default_user)
        return resolved.id, list(rows or [])

    async def _stamps_by_data_id(self, dataset: str) -> dict[str, dict[str, Any]]:
        """Map ``str(Data.id) -> external_metadata`` for citation resolution.

        Returns ``{}`` when the dataset can't be resolved, so citations degrade
        to text-only rather than failing.
        """
        _, rows = await self._dataset_rows(dataset)
        stamps: dict[str, dict[str, Any]] = {}
        for row in rows:
            stamp = getattr(row, "external_metadata", None)
            if isinstance(stamp, dict):
                stamps[str(row.id)] = stamp
        return stamps

    @staticmethod
    def _as_uuid(item_id: Optional[str]) -> Optional[UUID]:
        """Coerce a caller item id to the UUID cognee uses as ``Data.id``.

        Accepts a real UUID string as-is; derives a stable UUIDv5 from any other
        opaque id so ingestion stays idempotent on the caller's key.
        """
        if not item_id:
            return None
        try:
            return UUID(item_id)
        except (ValueError, AttributeError, TypeError):
            return uuid5(NAMESPACE_URL, item_id)

    @staticmethod
    def _to_citation(response: Any, stamps: dict[str, dict[str, Any]]) -> Citation:
        """Normalize a cognee ``RecallResponse`` (a tagged union) to a Citation.

        Handled generically via attribute access so it survives the union
        growing new member types: every member carries ``source``; graph
        entries carry ``text``/``score``/``metadata``; session entries carry
        ``answer``/``content``. When a result carries a ``data_id`` in its
        provenance metadata, the matching stamp yields the permalink and author
        for a citation; otherwise the citation is text-only.
        """
        source = getattr(response, "source", "graph")
        text = (
            getattr(response, "text", None)
            or getattr(response, "answer", None)
            or getattr(response, "content", None)
            or ""
        )

        metadata = getattr(response, "metadata", None)
        data_id = metadata.get("data_id") if isinstance(metadata, dict) else None
        stamp = stamps.get(str(data_id), {}) if data_id is not None else {}
        author = stamp.get("user")
        return Citation(
            text=text,
            source=source,
            score=getattr(response, "score", None),
            permalink=stamp.get("permalink"),
            user=str(author) if author is not None else None,
        )
