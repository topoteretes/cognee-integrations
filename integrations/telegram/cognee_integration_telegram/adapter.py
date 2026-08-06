"""Thin memory adapter: maps chat events onto cognee, nothing more.

The bot carries no memory logic of its own — it calls three primitives here,
which wrap a running cognee server's ``remember`` / ``recall`` / ``forget`` over
HTTP (via :class:`CogneeHttpClient`). Keeping this layer transport-agnostic means
the same core can back a Slack/Discord bot later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .citations import CitationLedger, MessageRef
from .http_client import CogneeHttpClient
from .scoping import Scope, resolve_scope

# recall() returns result objects; the renderable text lives in a type-specific
# field — ``answer`` for QA/session entries, ``text`` for graph hits, ``content``
# for context entries. Over HTTP these arrive as dicts; we read whichever is set.
_TEXT_ATTRS = ("answer", "text", "content")


def _answer_text(item: object) -> str:
    """Pull the renderable text out of one recall result item (dict or object)."""
    for attr in _TEXT_ATTRS:
        value = item.get(attr) if isinstance(item, dict) else getattr(item, attr, None)
        if value:
            return str(value)
    return ""


@dataclass
class Answer:
    """A recall answer plus its resolved Telegram citations."""

    text: str
    citations: list[MessageRef] = field(default_factory=list)


class CogneeMemoryAdapter:
    """Maps Telegram conversations onto cognee memory over HTTP."""

    def __init__(self, client: Optional[CogneeHttpClient] = None) -> None:
        self.client = client or CogneeHttpClient()
        self.ledger = CitationLedger()
        # chat_id -> capturing? Absent means "capturing" (opt-out model).
        self._capture: dict[int, bool] = {}

    # -- scoping ---------------------------------------------------------
    def scope_for(
        self, *, chat_type: str, chat_id: int, user_id: int, thread_id: int | None = None
    ) -> Scope:
        return resolve_scope(
            chat_type=chat_type, chat_id=chat_id, user_id=user_id, thread_id=thread_id
        )

    # -- opt-out ---------------------------------------------------------
    def is_opted_out(self, chat_id: int) -> bool:
        return not self._capture.get(chat_id, True)

    def opt_out(self, chat_id: int) -> None:
        self._capture[chat_id] = False

    def opt_in(self, chat_id: int) -> None:
        self._capture[chat_id] = True

    # -- ingest ----------------------------------------------------------
    async def ingest(self, scope: Scope, ref: MessageRef) -> bool:
        """Capture one message into the chat's durable graph.

        No-op when the chat has opted out. Uses ``remember(dataset_name=...)``
        (add + cognify), which creates the per-chat dataset and builds a
        queryable knowledge graph. The message is recorded in the citation ledger
        only after it is durably stored, so ``/ask`` never cites a message that
        failed to persist.
        """
        if self.is_opted_out(scope.chat_id):
            return False
        await self.client.remember(ref.attributed_text(), dataset_name=scope.dataset_name)
        self.ledger.record(scope.dataset_name, ref)
        return True

    # -- answer ----------------------------------------------------------
    async def answer(self, scope: Scope, query: str) -> Answer:
        """Recall an answer for ``query`` and resolve its Telegram citations.

        Returns an empty ``Answer`` when this chat has no memory yet: a missing
        dataset is a 4xx the client maps to no results. Only a genuinely failing
        backend (5xx / connection error) propagates, so the bot can show a
        distinct "backend down" message.
        """
        results = await self.client.recall(query, dataset_name=scope.dataset_name)
        texts = [text for text in (_answer_text(item) for item in results or []) if text]
        full_text = "\n\n".join(texts).strip()
        # include_references appends a grounded "Evidence:" block (the retrieved
        # source chunks) to the answer. Show only the answer, but resolve citations
        # from the Evidence block alone: it quotes what was actually retrieved, so a
        # "no information" answer — which carries no Evidence — is never cited.
        display_text, _, evidence = full_text.partition("\n\nEvidence:")
        citations = self.ledger.resolve(scope.dataset_name, evidence) if evidence.strip() else []
        return Answer(text=display_text.strip(), citations=citations)

    # -- forget ----------------------------------------------------------
    async def forget(self, scope: Scope) -> None:
        """Clear a chat's durable dataset (graph + vectors) and drop the ledger.

        Idempotent: clearing a chat that never captured anything is a no-op (the
        client treats a missing dataset's 4xx as already-clear), not an error.
        """
        await self.client.forget(dataset_name=scope.dataset_name)
        self.ledger.drop(scope.dataset_name)
