import asyncio

from cognee_integration_discord.adapter import ChatMemoryAdapter, RecallResult
from cognee_integration_discord.service import MemoryService


def run(coro):
    return asyncio.run(coro)


class FakeAdapter(ChatMemoryAdapter):
    """Records calls and returns a canned recall result (no cognee needed)."""

    def __init__(self, recall_result: RecallResult | None = None) -> None:
        self.remembered: list[dict] = []
        self.forgotten: list[dict] = []
        self._recall_result = recall_result or RecallResult(answer="", sources=[])

    async def remember(self, text, *, dataset, session, provenance=None):
        self.remembered.append(
            {"text": text, "dataset": dataset, "session": session, "provenance": provenance}
        )

    async def recall(self, query, *, dataset, session, top_k=5):
        return self._recall_result

    async def forget(self, *, dataset, everything=False):
        self.forgotten.append({"dataset": dataset, "everything": everything})


def test_disabled_channel_is_not_ingested():
    adapter = FakeAdapter()
    service = MemoryService(adapter)

    stored = run(service.ingest_message("g", "c", "m", "alice", "hello"))

    assert stored is False
    assert adapter.remembered == []


def test_enabled_channel_ingests_with_provenance_and_scoping():
    adapter = FakeAdapter()
    service = MemoryService(adapter)
    service.enable_channel("g", "c")

    stored = run(service.ingest_message("g", "c", "m", "alice", "hello world"))

    assert stored is True
    assert len(adapter.remembered) == 1
    record = adapter.remembered[0]
    assert record["dataset"] == "discord-guild-g"
    assert record["session"] == "discord-g-c"
    assert "https://discord.com/channels/g/c/m" in record["text"]
    assert "alice" in record["text"]
    assert "hello world" in record["text"]


def test_blank_message_is_skipped_even_when_enabled():
    adapter = FakeAdapter()
    service = MemoryService(adapter)
    service.enable_channel("g", "c")

    assert run(service.ingest_message("g", "c", "m", "alice", "   ")) is False
    assert adapter.remembered == []


def test_disable_channel_stops_ingestion():
    adapter = FakeAdapter()
    service = MemoryService(adapter)
    service.enable_channel("g", "c")
    service.disable_channel("g", "c")

    assert run(service.ingest_message("g", "c", "m", "alice", "hi")) is False


def test_answer_includes_citations_from_recall():
    snippet = "[source] https://discord.com/channels/1/2/3 — bob\nThe deploy runs at 5pm."
    adapter = FakeAdapter(RecallResult(answer="The deploy runs at 5pm.", sources=[snippet]))
    service = MemoryService(adapter)

    result = run(service.answer("g", "c", "when is the deploy"))

    assert result.found is True
    assert "The deploy runs at 5pm." in result.text
    assert "https://discord.com/channels/1/2/3" in result.text
    assert "**Sources:**" in result.text


def test_answer_with_empty_memory_reports_not_found():
    adapter = FakeAdapter(RecallResult(answer="", sources=[]))
    service = MemoryService(adapter)

    result = run(service.answer("g", "c", "anything"))

    assert result.found is False
    assert "don't have anything" in result.text


def test_forget_guild_targets_the_guild_dataset():
    adapter = FakeAdapter()
    service = MemoryService(adapter)

    run(service.forget_guild("g"))

    assert adapter.forgotten == [{"dataset": "discord-guild-g", "everything": False}]
