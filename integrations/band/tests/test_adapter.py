"""Tests for CogneeMemoryAdapter: recall injection, QA capture, improve, and the
band-sdk 6.x lifecycle (startup, cleanup ownership, teardown).

Runs against the real band-sdk types and runtime startup/disposal functions;
only the inner adapter and the Cognee client are fakes.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from band.core.simple_adapter import SimpleAdapter
from band.core.turn_lifecycle import bind_turn_task
from band.core.types import AgentInput, HistoryProvider, PlatformMessage
from band.runtime.startup import accept_adapter, dispose_adapter, start_adapter
from band.runtime.tools.schema import ToolCallOutcome
from cognee_band.adapter import CogneeMemoryAdapter, render_memory_block
from cognee_band.config import CogneeSettings


def make_msg(
    content="hello", message_type="text", room_id="room1", sender_type="User", sender_name="alice"
):
    return PlatformMessage(
        id="m1",
        room_id=room_id,
        content=content,
        sender_id="u1",
        sender_type=sender_type,
        sender_name=sender_name,
        message_type=message_type,
        metadata={},
        created_at=datetime.now(timezone.utc),
    )


class FakeTurn:
    """band-sdk's ``Turn``: ``detach()`` marks a turn released early."""

    def __init__(self):
        self.detached = False

    def detach(self):
        self.detached = True


class FakeTools:
    """Dispatches tool calls the way band-sdk's AgentTools does: the tool
    method is resolved on ``self``, so a wrapping proxy is bypassed."""

    _METHODS = {"band_send_message": "send_message"}

    def __init__(self):
        self.sent = []
        self.attachment_ids = []
        self.turn = FakeTurn()
        self.registered = []

    def register_turn_task(self, owner):
        self.registered.append(owner)

    async def send_message(self, content, mentions=None, *, attachment_ids=None):
        if content == "FAIL":
            raise RuntimeError("platform rejected the message")
        self.sent.append(content)
        self.attachment_ids.append(attachment_ids)

    async def send_event(self, content, message_type, metadata=None):
        pass

    async def lookup_peers(self):
        return []

    async def execute_tool_call_structured(self, tool_name, arguments):
        name = tool_name.removeprefix("band-")
        method = self._METHODS.get(name, name)
        try:
            value = await getattr(self, method)(**arguments)
        except Exception as error:
            return ToolCallOutcome(value=str(error), ok=False, error_message=str(error))
        return ToolCallOutcome(value=value, ok=True)

    async def execute_tool_call(self, tool_name, arguments):
        return (await self.execute_tool_call_structured(tool_name, arguments)).value


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


class ToolCallingInnerAdapter(FakeInnerAdapter):
    """Replies the way LLM adapters (Anthropic, Agno, Gemini, ...) do: through
    a ``band_send_message`` tool call rather than ``tools.send_message``."""

    def __init__(self, reply="the answer", tool_name="band_send_message", structured=True):
        super().__init__(reply=reply)
        self.tool_name = tool_name
        self.structured = structured

    async def on_event(self, inp):
        self.seen.append(inp)
        await inp.tools.execute_tool_call("lookup_peers", {})
        call = (
            inp.tools.execute_tool_call_structured
            if self.structured
            else inp.tools.execute_tool_call
        )
        await call(self.tool_name, {"content": self.reply, "mentions": ["alice"]})


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


async def run_turn(
    adapter, content="hello", message_type="text", room_id="room1", drain=True, **msg_fields
):
    tools = FakeTools()
    inp = AgentInput(
        msg=make_msg(content=content, message_type=message_type, room_id=room_id, **msg_fields),
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
    # The sender's own words lead; memory follows in a delimited block that
    # says it was not written by the sender.
    assert forwarded.msg.content.startswith("what milk?\n\n<cognee_memory>")
    assert forwarded.msg.content.endswith("</cognee_memory>")
    assert "- milk is oat" in forwarded.msg.content
    assert "not written by the sender" in forwarded.msg.content
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
    assert client.stored_qa == [("alice: what milk?", "the answer", "band-room1")]


async def test_no_qa_stored_when_inner_raises():
    class Exploding(FakeInnerAdapter):
        async def on_event(self, inp):
            self.seen.append(inp)
            raise RuntimeError("llm failed")

    adapter, _, client = make_adapter(inner=Exploding())
    with pytest.raises(RuntimeError):
        await run_turn(adapter, content="q")
    await adapter._drain_pending()
    assert client.stored_qa == []


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


# What cognee >= 1.6 returns for an only_context graph search: the whole prompt
# its own LLM would have received, with the retrieved context in the middle.
GRAPH_TEXT = (
    "alice: earlier turn\n"
    "The question is: `who owns the migration?` and here is the context provided "
    "with a set of relationships from a knowledge graph: "
    "`Ravit -- owns -- migration checklist`\n\n"
    "## Active session guidance\n- answer briefly"
)


def test_render_keeps_only_the_retrieved_context():
    item = {"source": "graph", "kind": "context", "text": GRAPH_TEXT, "raw": {}}
    block = render_memory_block([item], "who owns the migration?")
    assert "- Ravit -- owns -- migration checklist" in block
    assert "The question is" not in block
    assert "Active session guidance" not in block
    assert "earlier turn" not in block


def test_render_unparsed_text_is_kept_whole():
    block = render_memory_block([{"text": "bare context from an older server"}], "q")
    assert "- bare context from an older server" in block


def test_render_session_qa_and_nothing_raw():
    qa = {"source": "session", "question": "who owns it?", "answer": "Ravit", "qa_id": "x1"}
    unanswered = {"source": "session", "question": "hi", "answer": "", "qa_id": "x2"}
    block = render_memory_block([qa, unanswered, {"irrelevant": None}], "q")
    assert "- Q: who owns it?\nA: Ravit" in block
    assert "x2" not in block and "irrelevant" not in block
    assert render_memory_block([unanswered, {"irrelevant": None}], "q") == ""
    assert render_memory_block([]) == ""


def test_render_block_is_capped():
    block = render_memory_block(["a" * 500, "b" * 500, "c" * 500], max_chars=800)
    assert "a" * 500 in block
    assert "ccc" not in block
    assert block.count("…[memory truncated]") == 1


# -- reply capture ---------------------------------------------------------------


@pytest.mark.parametrize("structured", [True, False])
async def test_tool_call_reply_is_captured(structured):
    adapter, _, client = make_adapter(inner=ToolCallingInnerAdapter(structured=structured))
    tools = await run_turn(adapter, content="what milk?")
    assert tools.sent == ["the answer"]  # sent exactly once
    assert client.stored_qa == [("alice: what milk?", "the answer", "band-room1")]


async def test_mcp_prefixed_send_tool_is_captured():
    inner = ToolCallingInnerAdapter(tool_name="band-band_send_message")
    adapter, _, client = make_adapter(inner=inner)
    await run_turn(adapter, content="q")
    assert client.stored_qa == [("alice: q", "the answer", "band-room1")]


async def test_failed_tool_call_send_is_not_captured():
    adapter, _, client = make_adapter(inner=ToolCallingInnerAdapter(reply="FAIL"))
    await run_turn(adapter, content="q")
    assert client.stored_qa == []


async def test_send_message_passes_extra_kwargs_through():
    class AttachingInner(FakeInnerAdapter):
        async def on_event(self, inp):
            await inp.tools.send_message("see file", attachment_ids=["f1"])

    adapter, _, client = make_adapter(inner=AttachingInner())
    tools = await run_turn(adapter, content="q")
    assert tools.attachment_ids == [["f1"]]
    assert client.stored_qa == [("alice: q", "see file", "band-room1")]


# -- what reaches memory ---------------------------------------------------------


@pytest.mark.parametrize("content", ["/model gpt-5", "@bot /model gpt-5", "  @bot @other /help"])
async def test_commands_pass_through_untouched(content):
    # Codex parses local commands from the raw content in on_message: a memory
    # block in front of the "/" would turn the command into a prompt.
    adapter, inner, client = make_adapter(client=FakeClient(["memory that must not appear"]))
    await run_turn(adapter, content=content)
    assert inner.seen[0].msg.content == content
    assert client.recalls == [] and client.stored_qa == []


async def test_system_notices_get_no_memory():
    adapter, inner, client = make_adapter(client=FakeClient(["memory"]))
    content = "Received contact request from bob"
    await run_turn(adapter, content=content, sender_type="System", sender_name="Contact Events")
    assert inner.seen[0].msg.content == content
    assert client.recalls == [] and client.stored_qa == []


async def test_turn_without_reply_is_not_stored():
    # e.g. the model ends the turn with band_no_reply
    adapter, _, client = make_adapter(inner=FakeInnerAdapter(reply=""))
    await run_turn(adapter, content="thanks @bot")
    assert client.stored_qa == []


async def test_question_without_sender_name_is_stored_bare():
    adapter, _, client = make_adapter()
    await run_turn(adapter, content="q", sender_name=None)
    assert client.stored_qa == [("q", "the answer", "band-room1")]


# -- detached (approval-parked) turns ----------------------------------------------


class DetachingInner(FakeInnerAdapter):
    """Parks the turn on a human decision the way Codex / ClaudeSDK do: the
    worker is registered through band-sdk's ``bind_turn_task``, the turn is
    detached, ``on_event`` returns, and the reply is sent later."""

    def __init__(self, register=True):
        super().__init__()
        self.register = register
        self.approve = asyncio.Event()
        self.worker = None

    async def on_event(self, inp):
        self.seen.append(inp)
        tools = inp.tools

        async def work():
            await self.approve.wait()
            await tools.send_message("fixed it after approval")

        self.worker = asyncio.get_running_loop().create_task(work())
        if self.register:
            bind_turn_task(tools, self.worker)
        tools.turn.detach()


async def test_detached_turn_stores_the_late_reply():
    inner = DetachingInner()
    adapter, _, client = make_adapter(inner=inner)
    tools = await run_turn(adapter, content="fix the test")
    assert client.stored_qa == []  # nothing stored while parked
    assert len(tools.registered) == 1  # registration still reaches the real tools
    inner.approve.set()
    await inner.worker
    await asyncio.sleep(0)
    await adapter._drain_pending()
    assert client.stored_qa == [("alice: fix the test", "fixed it after approval", "band-room1")]


async def test_unregistered_detached_turn_is_stored_at_the_next_turn():
    # Cursor / OpenCode: no finish signal, so the room's next turn flushes it.
    inner = DetachingInner(register=False)
    adapter, _, client = make_adapter(inner=inner)
    await run_turn(adapter, content="fix the test")
    inner.approve.set()
    await inner.worker
    assert client.stored_qa == []
    inner.on_event = FakeInnerAdapter.on_event.__get__(inner)
    await run_turn(adapter, content="next")
    assert client.stored_qa[0] == ("alice: fix the test", "fixed it after approval", "band-room1")


async def test_unregistered_detached_turn_is_stored_at_cleanup():
    inner = DetachingInner(register=False)
    adapter, _, client = make_adapter(inner=inner)
    await run_turn(adapter, content="fix the test")
    inner.approve.set()
    await inner.worker
    await adapter.on_cleanup("room1")
    assert client.log == [("store", "band-room1"), ("improve", "band-room1")]


# -- startup ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "api_key", "warns"),
    [
        ("http://localhost:8011", "", True),
        ("http://127.0.0.1:8000", "", True),
        ("http://localhost:8011", "ck_x", False),
        ("https://tenant.cognee.ai", "", False),
    ],
)
async def test_local_server_without_key_warns(caplog, base_url, api_key, warns):
    settings = CogneeSettings(base_url=base_url, api_key=api_key, dataset="ds")
    adapter = CogneeMemoryAdapter(FakeInnerAdapter(), settings=settings, client=FakeClient())
    with caplog.at_level("WARNING", logger="cognee_band"):
        await start_adapter(adapter, agent_name="bot", agent_description="d", feature_flags={})
    assert any("COGNEE_API_KEY" in r.getMessage() for r in caplog.records) is warns
