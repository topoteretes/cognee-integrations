"""Thin chat-memory adapter interface for the Slack + cognee bot.

A thin, framework-agnostic ``ChatMemory`` interface plus the value types it works
with; the Slack transport layer depends only on this interface, so the concrete
backend (``cognee_memory.py``) can be swapped without touching the handlers.

Deliberately kept dependency-free: this module imports only the standard library.
It does **not** import Slack (``slack_bolt``), cognee, or httpx — those belong to
the concrete implementation and the transport layer, not the contract.

Session / dataset mapping:

* **Memory boundary = one dataset per channel** (``slack_<channel_id>``). This is
  the granularity at which cognee can actually forget (dataset-level delete), so
  it is the boundary that makes the ``forget`` story work.
* **Node-set tag = ``[channel_id]``** so chunk retrieval can be filtered to a
  single channel within a dataset (the ``node_name`` filter at search time).
* **Thread** — ``thread_ts`` is captured so an @mention asked inside a thread can
  be answered in that thread; it does not change the memory boundary.

Citations: since the HTTP API stores opaque text and strips arbitrary metadata
from the vector payload, the per-message ``permalink`` / ``author`` are carried
*inside* the stored text as a provenance header at ingest and parsed back out of
the retrieved chunk text at answer time (see ``cognee_memory.py``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ConversationRef:
    """Identity of a Slack conversation, and its mapping into cognee.

    Frozen (hashable) so it can be used as a dict/queue key.
    """

    team_id: str
    channel_id: str
    thread_ts: str | None = None

    @property
    def dataset_name(self) -> str:
        """cognee dataset that stores this channel's memory (the forget boundary)."""
        return f"slack_{self.channel_id}"

    @property
    def node_set(self) -> list[str]:
        """Node-set tag applied at ingest, used to filter chunk retrieval by channel."""
        return [self.channel_id]


@dataclass(frozen=True)
class Citation:
    """A single source message backing an answer.

    ``ok`` is ``False`` when the permalink is stale/missing; renderers must then
    fall back to plain text (``snippet``/``author``) instead of emitting a link.
    """

    channel_id: str
    ts: str
    permalink: str
    author: str
    snippet: str
    ok: bool = True


@dataclass(frozen=True)
class Answer:
    """A natural-language answer plus its deduplicated source citations."""

    text: str
    citations: list[Citation] = field(default_factory=list)


class ChatMemory(ABC):
    """The thin ingest / flush / answer / forget contract.

    Implementations wrap cognee's memory API (over HTTP). The Slack transport
    layer depends only on this abstract type, so the concrete backend can be
    replaced without touching the handlers.
    """

    @abstractmethod
    async def ingest(
        self,
        ref: ConversationRef,
        *,
        ts: str,
        text: str,
        permalink: str,
        author: str,
    ) -> None:
        """Buffer a single Slack message into the channel's memory.

        Implementations record the ``permalink``/``author`` so the message can
        later be cited (carried as a provenance header inside the stored text).
        Ingestion does not build the graph — that is deferred to :meth:`flush`
        (batch/triggered cognify).
        """

    @abstractmethod
    async def flush(self, ref: ConversationRef) -> None:
        """Make buffered messages for ``ref``'s channel searchable (trigger cognify)."""

    @abstractmethod
    async def answer(self, ref: ConversationRef, *, query: str) -> Answer:
        """Answer ``query`` from the channel's memory, with source citations."""

    @abstractmethod
    async def forget(self, ref: ConversationRef) -> None:
        """Delete all memory for ``ref``'s channel (dataset-level forget)."""
