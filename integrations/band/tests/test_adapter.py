"""Tests for CogneeMemoryAdapter: recall injection, QA capture, improve, and the
band-sdk 6.x lifecycle (startup, cleanup ownership, teardown).

Runs against the real band-sdk types and runtime startup/disposal functions;
only the inner adapter and the Cognee client are fakes.
"""

from datetime import datetime, timezone

import pytest
from band.core.simple_adapter import SimpleAdapter
from band.core.types import AgentInput, HistoryProvider, PlatformMessage
from band.runtime.startup import accept_adapter, dispose_adapter, start_adapter
from cognee_band.adapter import CogneeMemoryAdapter, render_memory_block
from cognee_band.config import CogneeSettings


def make_msg(content="hello", message_type="text", room_id="room1"):
    return PlatformMessage(
        id="m1",
        room_id=room_id,
        content=content,
        sender_id="u1",
        sender_type="User",
        sender_name="alice",
        message_type=message_type,
        metadata={},
        created_at=datetime.now(timezone.utc),
    )


class FakeTools:
    def __init__(self):
        self.sent = []

    async def send_message(self, content, mentions=None):
        self.sent.append(content)

    async def send_event(self, content, message_type, metadata=None):
        pass


class FakeInnerAdapter(SimpleAdapter[list]):
    """Records what reaches it and replies via tools.send_message."""

    def __init__(self, reply="the answer"):
        super().__init__()
        self.reply = reply
        self.seen = []
        self.calls = []
        self.cleaned_rooms = []

    async def startup(self, *args, **kwargs):
        self.calls.append("startup")
        await super().startup(*args, **kwargs)

    async def on_started(self, agent_name, agent_description):
        await super().on_started(agent_name, agent_description)
        self.calls.append(("on_started", agent_name, agent_description))

    async def on_event(self, inp):
        self.seen.append(inp)
        if self.reply:
            await inp.tools.send_message(self.reply)

    async def on_message(self, *args, **kwargs):
        raise AssertionError("turns are routed through on_event")

    async def on_cleanup(self, room_id):
        self.cleaned_rooms.append(room_id)

    async def cleanup_all(self):
        self.calls.append("cleanup_all")


class FakeClient:
    """Stands in for CogneeClient; records calls in order."""

    def __init__(self, recall_result=None, store_delay=0.0):
        self.recall_result = recall_result if recall_result is not None else []
        self.store_delay = store_delay
        self.recalls = []
        self.stored_qa = []
        self.improved = []
        self.log = []

    def recall(self, query, *, session_id="", top_k=0):
        self.recalls.append((query, session_id))
        return self.recall_result

    def store_qa(self, question, answer, *, session_id, context=""):
        if self.store_delay:
            import time

            time.sleep(self.store_delay)
        self.stored_qa.append((question, answer, session_id))
        self.log.append(("store", session_id))
        return {}

    def improve(self, session_id):
        self.improved.append(session_id)
        self.log.append(("improve", session_id))
        return {"ok": True}


def make_adapter(inner=None, client=None):
    settings = CogneeSettings(base_url="http://x", dataset="ds")
    inner = inner or FakeInnerAdapter()
    client = client or FakeClient()
    return CogneeMemoryAdapter(inner, settings=settings, client=client), inner, client


async def run_turn(adapter, content="hello", message_type="text", room_id="room1", drain=True):
    tools = FakeTools()
    inp = AgentInput(
        msg=make_msg(content=content, message_type=message_type, room_id=room_id),
        tools=tools,
        history=HistoryProvider(raw=[]),
        participants_msg=None,
        contacts_msg=None,
        is_session_bootstrap=False,
        room_id=room_id,
    )
    await adapter.on_event(inp)
    if drain:
        await adapter._drain_pending()
    return tools


# -- band-sdk 6.x lifecycle -----------------------------------------------------


def test_wrapper_is_a_simple_adapter():
    adapter, _, _ = make_adapter()
    assert isinstance(adapter, SimpleAdapter)


def test_rejects_non_simple_adapter():
    with pytest.raises(TypeError):
        CogneeMemoryAdapter(object(), settings=CogneeSettings(base_url="http://x"))


def test_adopts_inner_features():
    adapter, inner, _ = make_adapter()
    assert adapter.features is inner.features


async def test_runtime_startup_reaches_inner():
    adapter, inner, _ = make_adapter()
    adapter.platform = "CONN"  # the runtime injects this on the wrapper
    await start_adapter(adapter, agent_name="bot", agent_description="desc", feature_flags={})
    assert ("on_started", "bot", "desc") in inner.calls
    assert inner.platform == "CONN"
    assert inner.agent_name == "bot"


async def test_cleanup_ownership_and_registered_resources_reach_inner():
    adapter, inner, _ = make_adapter()
    released = []

    async def release():
        released.append(True)

    inner.add_cleanup(release)
    assert accept_adapter(adapter, owner := object()) is True
    await start_adapter(adapter, agent_name="bot", agent_description="d", feature_flags={})
    await dispose_adapter(adapter)
    assert "cleanup_all" in inner.calls
    assert released == [True]
    del owner


async def test_control_and_interrupt_forward_to_inner():
    class Controlled(FakeInnerAdapter):
        async def try_handle_control(self, msg, tools):
            self.calls.append("control")
            return True

        async def on_interrupt(self, room_id, mode):
            self.calls.append(("interrupt", room_id))

    adapter, inner, _ = make_adapter(inner=Controlled())
    assert await adapter.try_handle_control(make_msg(), FakeTools()) is True
    await adapter.on_interrupt("r1", None)
    assert inner.calls == ["control", ("interrupt", "r1")]


# -- memory behaviour --------------------------------------------------------------


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
    await run_turn(adapter, message_type="tool_result", content="x")
    assert inner.seen[0].msg.content == "x"
    assert client.recalls == [] and client.stored_qa == []


async def test_reply_still_reaches_platform_through_capture_proxy():
    adapter, _, _ = make_adapter()
    tools = await run_turn(adapter)
    assert tools.sent == ["the answer"]


async def test_on_cleanup_drains_room_writes_before_improve():
    adapter, inner, client = make_adapter(client=FakeClient(store_delay=0.2))
    await run_turn(adapter, room_id="r9", drain=False)  # write still in flight
    await adapter.on_cleanup("r9")
    assert inner.cleaned_rooms == ["r9"]
    assert client.log == [("store", "band-r9"), ("improve", "band-r9")]


async def test_on_cleanup_does_not_wait_for_other_rooms():
    adapter, _, client = make_adapter(client=FakeClient(store_delay=0.3))
    await run_turn(adapter, room_id="slow", drain=False)
    await adapter.on_cleanup("other")
    assert client.improved == ["band-other"]
    assert client.stored_qa == []  # the slow room's write has not landed yet
    await adapter._drain_pending()


async def test_cleanup_all_improves_active_rooms_after_drain():
    adapter, inner, client = make_adapter(client=FakeClient(store_delay=0.1))
    await run_turn(adapter, room_id="a", drain=False)
    await run_turn(adapter, room_id="b", drain=False)
    await start_adapter(adapter, agent_name="bot", agent_description="d", feature_flags={})
    await dispose_adapter(adapter)
    assert "cleanup_all" in inner.calls
    assert sorted(client.improved) == ["band-a", "band-b"]
    # QA stores landed before improve bridged them
    assert [kind for kind, _ in client.log] == ["store", "store", "improve", "improve"]


def test_render_memory_block_shapes():
    assert render_memory_block([]) == ""
    assert render_memory_block([{"irrelevant": None}]) != ""  # falls back to json dump
    block = render_memory_block(["fact one", {"text": "fact two"}])
    assert "- fact one" in block and "- fact two" in block
