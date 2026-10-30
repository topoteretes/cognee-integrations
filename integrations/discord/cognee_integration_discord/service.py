"""Platform-agnostic memory service.

Holds all the bot's memory behavior — channel opt-in, message ingestion,
question answering with citations, and forget — driven purely through a
``ChatMemoryAdapter``. It knows nothing about discord.py, so it is fully
unit-testable with a fake adapter, and could back a Slack/Telegram bot too.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import mapping
from .adapter import ChatMemoryAdapter

_NO_MEMORY = "I don't have anything in memory about that yet."


@dataclass
class AnswerResult:
    text: str
    found: bool


class MemoryService:
    """Bot memory behavior over a ChatMemoryAdapter.

    Channel opt-in is privacy-first: nothing is ingested until a channel is
    explicitly enabled (an admin action in the bot layer).
    """

    def __init__(self, adapter: ChatMemoryAdapter, enabled_channels=None) -> None:
        self._adapter = adapter
        self._enabled: set[tuple[str, str]] = set(enabled_channels or [])

    # -- Channel opt-in -------------------------------------------------------

    def enable_channel(self, guild_id, channel_id) -> None:
        self._enabled.add((str(guild_id), str(channel_id)))

    def disable_channel(self, guild_id, channel_id) -> None:
        self._enabled.discard((str(guild_id), str(channel_id)))

    def is_enabled(self, guild_id, channel_id) -> bool:
        return (str(guild_id), str(channel_id)) in self._enabled

    # -- Memory operations ----------------------------------------------------

    async def ingest_message(
        self, guild_id, channel_id, message_id, author: str, content: str
    ) -> bool:
        """Remember a message if its channel is opted in. Returns whether stored."""
        if not content or not content.strip():
            return False
        if not self.is_enabled(guild_id, channel_id):
            return False

        url = mapping.message_url(guild_id, channel_id, message_id)
        await self._adapter.remember(
            mapping.format_ingest_text(content, url, author),
            dataset=mapping.dataset_for_guild(guild_id),
            session=mapping.session_for_channel(guild_id, channel_id),
            provenance={"url": url, "author": author},
        )
        return True

    async def answer(self, guild_id, channel_id, question: str, top_k: int = 5) -> AnswerResult:
        """Recall an answer for a question, with a Sources footer of message links."""
        result = await self._adapter.recall(
            question,
            dataset=mapping.dataset_for_guild(guild_id),
            session=mapping.session_for_channel(guild_id, channel_id),
            top_k=top_k,
        )
        answer = (result.answer or "").strip()
        if not answer:
            return AnswerResult(text=_NO_MEMORY, found=False)

        citations = mapping.extract_citations(result.sources)
        return AnswerResult(text=mapping.format_answer(answer, citations), found=True)

    async def forget_guild(self, guild_id) -> None:
        """Forget everything cognee holds for this server."""
        await self._adapter.forget(dataset=mapping.dataset_for_guild(guild_id))
