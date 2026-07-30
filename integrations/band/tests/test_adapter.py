"""Tests for CogneeMemoryAdapter: recall injection, QA capture, improve, proxying.

Fakes mirror the verified band-sdk 1.5.0 shapes (frozen dataclasses AgentInput
and PlatformMessage; the Agent drives adapters via duck typing), so the tests
run without band-sdk installed.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest
from cognee_band.adapter import CogneeMemoryAdapter, render_memory_block
from cognee_band.config import CogneeSettings


@dataclass(frozen=True)
class PlatformMessage:
    id: str = "m1"
    room_id: str = "room1"
    content: str = "hello"
    sender_id: str = "u1"
    sender_type: str = "user"
    sender_name: str = "alice"
    message_type: str = "text"
    metadata: Any = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class AgentInput:
    msg: PlatformMessage
    tools: Any
    history: Any = None
    participants_msg: str = None
    contacts_msg: str = None
    is_session_bootstrap: bool = False
    room_id: str = "room1"


class FakeTools:
    def __init__(self):
        self.sent = []

    async def send_message(self, content, mentions=None):
        self.sent.append(content)

    async def send_event(self, content, message_type, metadata=None):
        pass


class FakeInnerAdapter:
    """Records what reaches it and replies via tools.send_message."""

    def __init__(self, reply="the answer"):
        self.reply = reply
        self.seen = []
        self.started = None
        self.cleaned_rooms = []
        self.cleanup_all_called = False

    async def on_started(self, agent_name, agent_description):
        self.started = (agent_name, agent_description)

    async def on_event(self, inp):
        self.seen.append(inp)
        if self.reply:
            await inp.tools.send_message(self.reply)

    async def on_cleanup(self, room_id):
        self.cleaned_rooms.append(room_id)

    async def cleanup_all(self):
        self.cleanup_all_called = True


class FakeClient:
    """Stands in for CogneeClient; records calls."""

    def __init__(self, recall_result=None):
        self.recall_result = recall_result if recall_result is not None else []
        self.recalls = []
        self.stored_qa = []
        self.improved = []

    def recall(self, query, *, session_id="", top_k=0):
        self.recalls.append((query, session_id))
        return self.recall_result

    def store_qa(self, question, answer, *, session_id, context=""):
        self.stored_qa.append((question, answer, session_id))
        return {}

    def improve(self, session_id):
        self.improved.append(session_id)
        return {"ok": True}


def make_adapter(inner=None, client=None):
    settings = CogneeSettings(base_url="http://x", dataset="ds")
    inner = inner or FakeInnerAdapter()
    client = client or FakeClient()
    return CogneeMemoryAdapter(inner, settings=settings, client=client), inner, client


async def run_turn(adapter, content="hello", message_type="text", room_id="room1"):
    tools = FakeTools()
    inp = AgentInput(
        msg=PlatformMessage(content=content, message_type=message_type, room_id=room_id),
        tools=tools,
        room_id=room_id,
    )
    await adapter.on_event(inp)
    await adapter._drain_pending()
    return tools


async def test_recall_injected_above_message():
    adapter, inner, client = make_adapter(client=FakeClient([{"text": "milk is oat"}]))
    await run_turn(adapter, content="what milk?")
    forwarded = inner.seen[0]
    assert "milk is oat" in forwarded.msg.content
    assert forwarded.msg.content.endswith("what milk?")
    assert "Cognee memory" in forwarded.msg.content
    assert client.recalls == [("what milk?", "band-room1")]


async def test_empty_recall_leaves_message_untouched():
    adapter, inner, _ = make_adapter(client=FakeClient([]))
    await run_turn(adapter, content="hi")
    assert inner.seen[0].msg.content == "hi"


async def test_recall_failure_does_not_break_turn():
    adapter, inner, _ = make_adapter(client=FakeClient("UNREACHABLE"))
    tools = await run_turn(adapter, content="hi")
    assert inner.seen[0].msg.content == "hi"
    assert tools.sent == ["the answer"]


async def test_error_envelope_recall_is_not_injected():
    adapter, inner, _ = make_adapter(client=FakeClient({"error": "boom", "status": 500}))
    await run_turn(adapter, content="hi")
    assert inner.seen[0].msg.content == "hi"


async def test_qa_stored_with_original_question_and_captured_answer():
    adapter, _, client = make_adapter(client=FakeClient([{"text": "ctx"}]))
    await run_turn(adapter, content="what milk?")
    assert client.stored_qa == [("what milk?", "the answer", "band-room1")]


async def test_qa_stored_even_when_inner_raises():
    class Exploding(FakeInnerAdapter):
        async def on_event(self, inp):
            self.seen.append(inp)
            raise RuntimeError("llm failed")

    adapter, _, client = make_adapter(inner=Exploding())
    with pytest.raises(RuntimeError):
        await run_turn(adapter, content="q")
    await adapter._drain_pending()
    assert client.stored_qa == [("q", "", "band-room1")]


async def test_non_text_messages_pass_through_without_memory():
    adapter, inner, client = make_adapter()
    tools = FakeTools()
    inp = AgentInput(
        msg=PlatformMessage(message_type="tool_result", content="x"),
        tools=tools,
    )
    await adapter.on_event(inp)
    assert inner.seen[0] is inp  # untouched, same object
    assert client.recalls == [] and client.stored_qa == []


async def test_reply_still_reaches_platform_through_capture_proxy():
    adapter, _, _ = make_adapter()
    tools = await run_turn(adapter)
    assert tools.sent == ["the answer"]


async def test_on_cleanup_improves_room_session():
    adapter, inner, client = make_adapter()
    await run_turn(adapter, room_id="r9")
    await adapter.on_cleanup("r9")
    assert inner.cleaned_rooms == ["r9"]
    assert client.improved == ["band-r9"]


async def test_cleanup_all_improves_active_rooms_after_drain():
    adapter, inner, client = make_adapter()
    await run_turn(adapter, room_id="a")
    await run_turn(adapter, room_id="b")
    await adapter.cleanup_all()
    assert inner.cleanup_all_called
    assert sorted(client.improved) == ["band-a", "band-b"]
    # QA stores landed before improve bridged them
    assert len(client.stored_qa) == 2


async def test_attribute_proxying_both_directions():
    adapter, inner, _ = make_adapter()
    # Band's Agent.start does setattr(adapter, "_band_agent_id", ...)
    adapter._band_agent_id = "agent-42"
    assert inner._band_agent_id == "agent-42"
    inner.some_flag = True
    assert adapter.some_flag is True


async def test_on_started_delegates():
    adapter, inner, _ = make_adapter()
    await adapter.on_started("bot", "desc")
    assert inner.started == ("bot", "desc")


def test_render_memory_block_shapes():
    assert render_memory_block([]) == ""
    assert render_memory_block([{"irrelevant": None}]) != ""  # falls back to json dump
    block = render_memory_block(["fact one", {"text": "fact two"}])
    assert "- fact one" in block and "- fact two" in block
