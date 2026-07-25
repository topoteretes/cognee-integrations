"""cognee-backed implementation of the thin ``ChatMemory`` adapter.

The concrete backend behind the Slack bot's ingest / flush / answer / forget
contract. It talks to a running cognee server over HTTP (via
:class:`CogneeHttpClient`) — no in-process cognee. When the #3608 chat-memory
core lands, this module is the single thing that gets swapped; the Slack
handlers keep talking to :class:`ChatMemory`.

Mapping onto the cognee HTTP API:

* ingest  → ``POST /api/v1/add`` (dataset ``slack_<channel>``, ``node_set=[channel]``);
* flush   → ``POST /api/v1/cognify`` (batched graph build);
* answer  → two ``POST /api/v1/search`` calls: GRAPH_COMPLETION for the prose
            answer, CHUNKS (filtered by ``node_name=[channel]``) for citable
            source messages;
* forget  → ``POST /api/v1/forget`` (dataset-level delete).

Citations without a controllable data id
-----------------------------------------
The SDK could set ``DataItem.data_id`` so a retrieved chunk's ``document_id``
joined back to the source message. The HTTP ``/add`` endpoint has no such knob,
and cognee strips arbitrary metadata from the vector payload — only the chunk
*text* survives a CHUNKS search. So the per-message ``permalink`` / ``author``
ride inside the stored text as a compact provenance header at ingest, and are
parsed back out of the retrieved chunk text here. A chunk with no header (or a
blank permalink) degrades to a plain-text citation — never a broken link.
"""

from __future__ import annotations

import re
from typing import Any

from .http_client import CogneeHttpClient
from .memory_adapter import Answer, ChatMemory, Citation, ConversationRef

_CHUNK_TEXT_KEY = "text"
DEFAULT_TOP_K = 10
SNIPPET_MAX_CHARS = 200

# Provenance header embedded at the top of every stored message. Values are Slack
# ids / a URL (no spaces), so a space-delimited key=value line round-trips through
# cognee's chunking and comes back in the CHUNKS search text. A missing value is
# written as "-" so the field is always present and unambiguous.
_PROVENANCE_PREFIX = "[cognee-slack]"
_PROVENANCE_RE = re.compile(
    r"\[cognee-slack\][ \t]*channel=(?P<channel>\S*)[ \t]+ts=(?P<ts>\S*)"
    r"[ \t]+author=(?P<author>\S*)[ \t]+permalink=(?P<permalink>\S*)[^\n]*\n?"
)


def _encode_provenance(text: str, *, channel_id: str, ts: str, author: str, permalink: str) -> str:
    """Prefix a message with a parseable provenance header for citation recovery."""
    header = (
        f"{_PROVENANCE_PREFIX} channel={channel_id or '-'} ts={ts or '-'} "
        f"author={author or '-'} permalink={permalink or '-'}"
    )
    return f"{header}\n{text}"


def _decode_provenance(chunk_text: str) -> tuple[str, dict[str, str]]:
    """Split a retrieved chunk into (clean_snippet, provenance fields)."""
    match = _PROVENANCE_RE.search(chunk_text or "")
    if not match:
        return (chunk_text or "").strip(), {}
    fields = {
        key: ("" if value in (None, "-") else value) for key, value in match.groupdict().items()
    }
    clean = (chunk_text[: match.start()] + chunk_text[match.end() :]).strip()
    return clean, fields


def _first_text(results: Any) -> str:
    """Extract the natural-language answer string from a ``search`` return.

    ``search`` returns a list of results whose exact shape varies:

    * non-access-control (single-user) mode: a flat list — e.g. ``["answer"]``
      for GRAPH_COMPLETION;
    * access-control mode: ``[{"search_result": <result>, "dataset_id": ...}]``.

    We walk either shape and return the first non-empty string found.
    """
    if isinstance(results, str):
        return results
    if isinstance(results, dict):
        return _first_text(results.get("search_result"))
    if isinstance(results, (list, tuple)):
        for item in results:
            text = _first_text(item)
            if text:
                return text
    return ""


def _normalize_chunk_payloads(results: Any) -> list[dict]:
    """Flatten a ``search(CHUNKS)`` return into a list of payload dicts.

    Handles both the flat ``[chunk_dict, ...]`` (single-user) shape and the
    access-control ``[{"search_result": [chunk_dict, ...]}, ...]`` wrapper.
    """
    payloads: list[dict] = []
    if results is None:
        return payloads
    if isinstance(results, dict):
        if "search_result" in results:
            return _normalize_chunk_payloads(results["search_result"])
        return [results]
    if isinstance(results, (list, tuple)):
        for item in results:
            if isinstance(item, dict) and "search_result" in item:
                payloads.extend(_normalize_chunk_payloads(item["search_result"]))
            elif isinstance(item, dict):
                payloads.append(item)
            elif isinstance(item, (list, tuple)):
                payloads.extend(_normalize_chunk_payloads(item))
    return payloads


def _build_citations(chunk_payloads: list[dict]) -> list[Citation]:
    """Map chunk payloads → deduplicated ``Citation`` objects via embedded provenance.

    * Parse each chunk's text for the provenance header written at ingest.
    * Dedupe so multiple chunks of one message collapse to a single citation
      (keyed by permalink, or by author+ts+snippet when there is no permalink).
    * A chunk with no header or a blank permalink degrades to a plain-text
      citation (``ok=False``) — never a broken link, never a crash.
    """
    citations: list[Citation] = []
    seen: set[str] = set()
    for payload in chunk_payloads:
        chunk_text = payload.get(_CHUNK_TEXT_KEY, "") if isinstance(payload, dict) else ""
        snippet, prov = _decode_provenance(chunk_text)
        permalink = prov.get("permalink", "")
        author = prov.get("author", "")
        ts = prov.get("ts", "")
        channel_id = prov.get("channel", "")
        key = permalink or f"{author}:{ts}:{snippet[:40]}"
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            Citation(
                channel_id=channel_id,
                ts=ts,
                permalink=permalink,
                author=author,
                snippet=snippet[:SNIPPET_MAX_CHARS],
                ok=bool(permalink),
            )
        )
    return citations


class CogneeChatMemory(ChatMemory):
    """``ChatMemory`` backed by cognee's HTTP add / cognify / search / forget API."""

    def __init__(self, client: CogneeHttpClient | None = None, *, top_k: int = DEFAULT_TOP_K):
        self._client = client or CogneeHttpClient()
        self._top_k = top_k

    async def ingest(
        self,
        ref: ConversationRef,
        *,
        ts: str,
        text: str,
        permalink: str,
        author: str,
    ) -> None:
        """Add one message to the channel dataset with an embedded citation header.

        Does NOT cognify — that is deferred to :meth:`flush` (batch trigger).
        """
        stored_text = _encode_provenance(
            text, channel_id=ref.channel_id, ts=ts, author=author, permalink=permalink
        )
        await self._client.add(stored_text, dataset_name=ref.dataset_name, node_set=ref.node_set)

    async def flush(self, ref: ConversationRef) -> None:
        """Build the knowledge graph for the channel dataset (batch cognify)."""
        await self._client.cognify(dataset_name=ref.dataset_name)

    async def answer(self, ref: ConversationRef, *, query: str) -> Answer:
        """Answer ``query`` from the channel's memory, with source citations.

        Two searches: GRAPH_COMPLETION for the prose answer, CHUNKS (filtered to
        this channel's node set) for the citable source messages. A channel with
        nothing ingested/cognified yet has no dataset; the client maps that
        (a 4xx) to no results, so the renderer shows a calm "no memory yet" reply.
        """
        prose_results = await self._client.search(
            query,
            search_type="GRAPH_COMPLETION",
            dataset_name=ref.dataset_name,
            top_k=self._top_k,
        )
        chunk_results = await self._client.search(
            query,
            search_type="CHUNKS",
            dataset_name=ref.dataset_name,
            node_name=ref.node_set,
            top_k=self._top_k,
        )
        answer_text = _first_text(prose_results)
        citations = _build_citations(_normalize_chunk_payloads(chunk_results))
        return Answer(text=answer_text, citations=citations)

    async def forget(self, ref: ConversationRef) -> None:
        """Delete all memory for the channel (dataset-level forget).

        Idempotent: forgetting a never-used channel is a no-op (a missing dataset
        is a 4xx the client swallows).
        """
        await self._client.forget(dataset_name=ref.dataset_name)
