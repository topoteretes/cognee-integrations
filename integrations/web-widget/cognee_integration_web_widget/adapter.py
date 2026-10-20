"""Framework-agnostic chat-memory adapter for cognee-powered bots.

Every transport (web chat widget, WhatsApp, MS Teams, ...) plugs into this
thin layer so each bot stays small and they all share one memory model. The
adapter maps a platform conversation to a stable cognee ``session_id`` and
delegates to a running cognee server's memory API over HTTP (via
:class:`CogneeHttpClient`) — it never imports cognee in-process.

Session convention::

    web:{site_id}:{visitor_id}:{conversation_id}

Memory boundary (default): **per visitor-conversation**. Recalling with a
``session_id`` lets cognee's session-aware recall both use *and persist* that
conversation's history, so one visitor's chat never leaks into another's. An
optional shared, read-only *docs* dataset (``web:{site_id}:docs``) powers the
"ask our docs" mode: every conversation can recall from it, none writes to it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, List, Optional, Sequence

from .citations import Citation, split_evidence
from .code_refs import CodeRef, code_context_block, extract_code_seeds, parse_code_entry
from .http_client import CogneeHttpClient

logger = logging.getLogger("web_widget.adapter")

# cognee tags each recall entry with its origin. The answer to the *current*
# query is a graph/graph_context completion; a stored session entry is a prior
# turn, never the answer to display.
_ANSWER_SOURCES = ("graph", "graph_context")
# Code-graph facts are structured query results (JSON), not prose: they are
# reported as related code next to the answer, never shown as the answer text.
_NEVER_ANSWER_SOURCES = ("code",)
_EMPTY_ANSWER = "I don't have anything in memory for that yet."
# A code lookup that has not answered by then is dropped rather than holding
# the reply back.
CODE_LOOKUP_TIMEOUT = 20.0


@dataclasses.dataclass(frozen=True)
class Conversation:
    """A platform conversation, resolved to a cognee session."""

    site_id: str
    visitor_id: str
    conversation_id: str

    @property
    def session_id(self) -> str:
        return f"web:{self.site_id}:{self.visitor_id}:{self.conversation_id}"


@dataclasses.dataclass
class Answer:
    """A recalled answer plus the sources that back it."""

    text: str
    citations: List[Citation]
    session_id: str
    code: List[CodeRef] = dataclasses.field(default_factory=list)

    def as_dict(self) -> dict:
        # "answer" is the wire name the widget and README use for the prose.
        return {
            "answer": self.text,
            "citations": [c.as_dict() for c in self.citations],
            "code": [c.as_dict() for c in self.code],
            "session_id": self.session_id,
        }


def _field(entry: Any, name: str) -> Any:
    """Read a field from a recall entry (a Pydantic model or a plain dict)."""
    return entry.get(name) if isinstance(entry, dict) else getattr(entry, name, None)


def _entry_text(entry: Any) -> str:
    for key in ("text", "answer", "content"):
        value = _field(entry, key)
        if value and str(value).strip():
            return str(value).strip()
    return ""


def _answer_text(results: Sequence[Any]) -> str:
    """Pick the answer to display: the generated completion, else any hit."""
    entries = list(results or [])
    # Prefer the freshly generated completion (it carries the Evidence block);
    # fall back to any entry so a plain session-memory recall still answers.
    for generated_only in (True, False):
        for entry in entries:
            if generated_only and _field(entry, "source") not in _ANSWER_SOURCES:
                continue
            if _field(entry, "source") in _NEVER_ANSWER_SOURCES:
                continue
            text = _entry_text(entry)
            if text:
                return text
    return _EMPTY_ANSWER


class ChatMemoryAdapter:
    """Thin answer / seed-docs / forget layer over a cognee server (HTTP)."""

    def __init__(
        self,
        *,
        top_k: int = 8,
        client: Optional[CogneeHttpClient] = None,
        docs_base_url: Optional[str] = None,
        code_base_url: Optional[str] = None,
    ) -> None:
        self.top_k = top_k
        self.client = client or CogneeHttpClient()
        # Where the ingested pages are published, so a citation can link to
        # the page a reader can actually open instead of naming a chunk.
        self.docs_base_url = docs_base_url or None
        # Where the ingested repository can be browsed (a blob URL), so related
        # code links to the file and line.
        self.code_base_url = code_base_url or None

    # -- session helpers ---------------------------------------------------

    def conversation(self, *, site_id: str, visitor_id: str, conversation_id: str) -> Conversation:
        return Conversation(site_id=site_id, visitor_id=visitor_id, conversation_id=conversation_id)

    def docs_dataset(self, site_id: str) -> str:
        return f"web:{site_id}:docs"

    def conversations_dataset(self, site_id: str) -> str:
        """Where distillation moves conversations into permanent memory.

        cognee's persisted copy of each distilled session and the lessons drawn
        from it: one dataset, so a lesson cannot outlive the conversation it
        cites and one clear disposes of all of it.

        Deliberately not the docs corpus. ``answer`` names the docs dataset and
        nothing else, so nothing stored here can become the source of a later
        answer - which is the failure that matters: one wrong reply quoted back
        as evidence for the next.
        """
        return f"web:{site_id}:conversations"

    # -- "ask our docs" corpus ---------------------------------------------

    async def ingest_docs(self, *, site_id: str, documents: Sequence[str]) -> None:
        """Seed the shared, read-only "ask our docs" corpus for a site."""
        dataset = self.docs_dataset(site_id)
        for doc in (d for d in documents if d and d.strip()):
            # No session_id: the docs corpus is shared, not conversation-scoped.
            await self.client.remember(doc, dataset_name=dataset)

    # -- answer (recall + citations) ---------------------------------------

    async def answer(
        self,
        *,
        conversation: Conversation,
        query: str,
        remember: bool = True,
    ) -> Answer:
        """Answer a query, scoped to this conversation and the docs corpus.

        With ``remember=True`` the ``session_id`` is passed so cognee's
        session-aware recall both uses and persists this conversation's history.
        ``remember=False`` is the opt-out: the turn is answered statelessly and
        nothing is stored.

        The turn is stored by the caller, not here: ``answer`` stays a read.

        A docs corpus that was never seeded is reported by the server as a 4xx,
        which the HTTP client maps to no results — so the widget degrades to an
        "empty memory" answer rather than erroring.
        """
        session_id = conversation.session_id if remember else None
        # The dataset list is fixed here rather than taken from the caller.
        # ``None`` means "every dataset this key can read", which would let the
        # conversations corpus - and anything distilled from it - answer a
        # visitor. Structural, not a default someone can switch off.
        datasets = [self.docs_dataset(conversation.site_id)]
        # Code first: what the message names in the repository is told to the
        # answering model through this turn's system prompt. Only a message that
        # names code waits for the lookups, which make no LLM call.
        looked_up = await self._code_lookups(query, datasets)
        results = await self.client.recall(
            query,
            datasets=datasets,
            session_id=session_id,
            top_k=self.top_k,
            system_prompt_extra=code_context_block(looked_up) or None,
        )
        text, citations = split_evidence(_answer_text(results), self.docs_base_url)
        # Code entries can come from the answer's own recall too, when its
        # scope includes "code"; either way each node is reported once.
        code: List[CodeRef] = []
        for entry in [*results, *looked_up]:
            if _field(entry, "source") != "code":
                continue
            ref = parse_code_entry(entry, self.code_base_url)
            if ref and all(ref.symbol != seen.symbol for seen in code):
                code.append(ref)
        return Answer(text=text, citations=citations, session_id=conversation.session_id, code=code)

    async def _code_lookups(self, query: str, datasets: List[str]) -> List[Any]:
        """Code-graph facts for the names in ``query``. Never fails the answer."""
        seeds = extract_code_seeds(query)
        if not seeds:
            return []

        async def one(seed: str) -> List[Any]:
            try:
                return await asyncio.wait_for(
                    self.client.code_lookup(seed, datasets=datasets), CODE_LOOKUP_TIMEOUT
                )
            except Exception as error:  # noqa: BLE001 - related code is optional
                logger.info("web_widget: code lookup for %r skipped: %s", seed, error)
                return []

        found = await asyncio.gather(*(one(seed) for seed in seeds))
        return [entry for entries in found for entry in entries]

    # -- forget ------------------------------------------------------------

    async def forget(self, *, conversation: Conversation) -> bool:
        """Best-effort clear of a single conversation's memory.

        Over HTTP a conversation is cleared by forgetting the dataset that
        carries its name; a dataset that was never written to is a no-op (the
        server reports 4xx, which the client treats as already-clear). Returns
        ``True`` when the call did not error.
        """
        await self.client.forget(dataset_name=conversation.session_id)
        return True
