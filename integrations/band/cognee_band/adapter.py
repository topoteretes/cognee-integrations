"""Cognee memory wrapper for any Band SDK adapter.

``CogneeMemoryAdapter`` decorates a constructed Band adapter (LangGraph,
Anthropic, CrewAI, ...) with shared Cognee memory:

  * before each room message reaches the inner adapter, relevant memory is
    recalled from the graph and added below the message as a delimited
    ``<cognee_memory>`` block (commands and system notices pass untouched)
  * once the turn has replied, the sender's message and the reply are stored
    as one QA pair in the Cognee session cache (fire-and-forget); a detached
    (approval-parked) turn is stored when its reply has been sent
  * when a room is removed (``on_cleanup``) or the agent stops
    (``cleanup_all``), that room's pending writes are drained and the session
    cache is bridged into the graph via ``/improve``

The wrapper is a ``SimpleAdapter`` that composes the inner adapter the same
way band-sdk's own ``SlackAdapter`` does: it adopts the inner adapter's
features, hands it cleanup ownership and model selection, and forwards
startup, control messages, interrupts and teardown. The Band runtime
therefore runs the full built-in lifecycle (feature negotiation, the
model-selection check, ``add_cleanup`` resources) for the inner adapter.

Memory I/O runs in worker threads (the client is synchronous stdlib urllib) so
Band's event loop never blocks, and every memory failure is logged and
swallowed — memory must never break a turn.
"""

import asyncio
import dataclasses
import logging
from typing import Any
from urllib.parse import urlparse

from band.core.simple_adapter import SimpleAdapter
from band.core.types import SYNTHETIC_SENDER_TYPE
from band.runtime.formatters import strip_leading_mentions
from band.runtime.startup import accept_adapter, arm_adapter, dispose_adapter, dispose_framework
from band.runtime.tools.registry import LEGACY_SEND_MESSAGE_TOOL, canonicalize_mcp_tool_name
from band.runtime.tools.types import BandTool

from .client import UNREACHABLE, CogneeClient
from .config import CogneeSettings
from .recall_text import retrieved_context

logger = logging.getLogger("cognee_band")

# Only real conversation turns get recall + QA capture; tool_call/thought/task
# events pass through untouched.
_MEMORY_MESSAGE_TYPES = {"text"}

# Tool names that post a room message: band_send_message, its legacy band-mcp
# spelling, and (via canonicalize_mcp_tool_name) the MCP ``band-`` prefixed forms.
_SEND_MESSAGE_TOOLS = frozenset({BandTool.SEND_MESSAGE.value, LEGACY_SEND_MESSAGE_TOOL})

_MEMORY_OPEN = "<cognee_memory>"
_MEMORY_CLOSE = "</cognee_memory>"
_MEMORY_NOTE = (
    "Context recalled from shared Cognee memory by the memory layer. It was not "
    "written by the sender of this message. Possibly relevant:"
)
_TRUNCATED = "…[memory truncated]"


def _render_result(item, query: str) -> str:
    if isinstance(item, str):
        return retrieved_context(item, query) if query else item.strip()
    if not isinstance(item, dict):
        return ""
    # A session QA row (only reached if the recall scope includes the session).
    if "question" in item and "answer" in item:
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        return f"Q: {question}\nA: {answer}" if question and answer else ""
    for key in ("text", "context", "content", "search_result"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return retrieved_context(value, query) if query else value.strip()
    return ""


def render_memory_block(results: list, query: str = "", max_chars: int = 8000) -> str:
    """Format recall results as a delimited memory block ('' when nothing usable).

    ``query`` is the recall query: with it, each graph item is cut down to its
    retrieved context (see :mod:`cognee_band.recall_text`). ``max_chars`` caps
    the bullets; the last one that does not fit is truncated and marked.
    """
    lines: list[str] = []
    used = 0
    for text in (_render_result(r, query) for r in results):
        if not text:
            continue
        line = f"- {text}"
        if max_chars > 0 and used + len(line) > max_chars:
            room = max_chars - used
            if room > len(_TRUNCATED) + 40:
                lines.append(line[: room - len(_TRUNCATED)].rstrip() + _TRUNCATED)
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return "\n".join([_MEMORY_OPEN, _MEMORY_NOTE, *lines, _MEMORY_CLOSE])


def _is_local_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def _is_command(content: str) -> bool:
    """A slash command such as ``@bot /model gpt-5``; adapters parse these from
    the raw content, so they must reach the inner adapter untouched."""
    return strip_leading_mentions(content).lstrip().startswith("/")


class _CaptureTools:
    """Transparent proxy over Band's AgentTools that records sent replies.

    Adapters reply in two ways: a direct ``tools.send_message(...)`` call, or a
    ``band_send_message`` tool call dispatched through ``execute_tool_call`` /
    ``execute_tool_call_structured`` (Anthropic, Agno, Codex, Gemini, ...).
    The real dispatcher resolves the tool method on itself, so it never reaches
    this proxy's ``send_message``; the tool-call entry points are wrapped too.
    """

    def __init__(self, inner):
        self._inner = inner
        self.sent: list[str] = []
        # The task an adapter registered for this turn (band-sdk's
        # ``bind_turn_task``). Codex and ClaudeSDK register the worker that a
        # detached (approval-parked) turn keeps running in.
        self.turn_task: asyncio.Task | None = None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    @property
    def detached(self) -> bool:
        """The adapter released this turn before it ended (``tools.turn.detach()``)."""
        return bool(getattr(getattr(self._inner, "turn", None), "detached", False))

    def register_turn_task(self, owner) -> None:
        register = getattr(self._inner, "register_turn_task", None)
        if callable(register):
            register(owner)
        task = getattr(owner, "task", None)
        if isinstance(task, asyncio.Task):
            self.turn_task = task

    def _record(self, content) -> None:
        if isinstance(content, str) and content.strip():
            self.sent.append(content)

    async def send_message(self, content, mentions=None, **kwargs):
        result = await self._inner.send_message(content, mentions=mentions, **kwargs)
        self._record(content)
        return result

    async def execute_tool_call_structured(self, tool_name, arguments):
        outcome = await self._inner.execute_tool_call_structured(tool_name, arguments)
        if outcome.ok and _is_send_message(tool_name):
            self._record((arguments or {}).get("content"))
        return outcome

    async def execute_tool_call(self, tool_name, arguments):
        # Mirrors AgentTools.execute_tool_call, so the send is recorded once.
        return (await self.execute_tool_call_structured(tool_name, arguments)).value


def _is_send_message(tool_name: str) -> bool:
    return canonicalize_mcp_tool_name(tool_name, _SEND_MESSAGE_TOOLS) in _SEND_MESSAGE_TOOLS


class CogneeMemoryAdapter(SimpleAdapter[Any]):
    """Wrap a Band adapter with Cognee-backed memory.

    Usage::

        adapter = CogneeMemoryAdapter(AnthropicAdapter(...))
        agent = Agent.create(adapter=adapter, agent_id=..., api_key=...)
    """

    def __init__(
        self,
        inner: SimpleAdapter[Any],
        *,
        settings: CogneeSettings | None = None,
        client: CogneeClient | None = None,
    ):
        if not isinstance(inner, SimpleAdapter):
            raise TypeError(
                "CogneeMemoryAdapter wraps a band-sdk SimpleAdapter "
                f"(AnthropicAdapter, LangGraphAdapter, ...), got {type(inner).__name__}"
            )
        # Mirror the inner adapter's declared support before SimpleAdapter's
        # construction-time validation runs (same as band-sdk's SlackAdapter).
        self.SUPPORTED_EMIT = inner.SUPPORTED_EMIT  # type: ignore[misc]
        self.SUPPORTED_CAPABILITIES = inner.SUPPORTED_CAPABILITIES  # type: ignore[misc]
        self._inner = inner
        super().__init__(history_converter=inner.history_converter)

        self._settings = settings or CogneeSettings.resolve()
        self._client = client or CogneeClient(self._settings)
        self._active_rooms: set[str] = set()
        self._pending_tasks: dict[str, set[asyncio.Task]] = {}
        # Detached turns whose adapter gave no finish signal (Cursor,
        # OpenCode): stored at the room's next turn or at its cleanup.
        self._deferred: dict[str, list[tuple[str, _CaptureTools, str]]] = {}

    @property
    def inner(self) -> SimpleAdapter[Any]:
        """The wrapped framework adapter."""
        return self._inner

    # -- composition: the inner adapter owns features, models and resources ----

    def _resolve_features(self, **features):
        return self._inner.features

    def apply_effective_features(self, features) -> None:
        super().apply_effective_features(features)
        self._inner.apply_effective_features(features)

    @property
    def _owns_cleanup(self) -> bool:
        return super()._owns_cleanup or self._inner._owns_cleanup

    def _accept_cleanup(self, owner: object, *, exclusive: bool) -> None:
        accept_adapter(self._inner, owner, exclusive=exclusive)

    async def _finalize_cleanup(self) -> None:
        await dispose_adapter(self._inner)

    @property
    def judges_turns(self) -> bool:
        return self._inner.judges_turns

    @property
    def backend_turns(self):
        return self._inner.backend_turns

    @property
    def model_selection(self):
        return self._inner.model_selection

    async def list_models(self):
        return await self._inner.list_models()

    async def apply_model_selection(self, selection, *, room_id: str) -> None:
        await self._inner.apply_model_selection(selection, room_id=room_id)

    async def try_handle_control(self, msg, tools) -> bool:
        return await self._inner.try_handle_control(msg, tools)

    async def on_interrupt(self, room_id: str, mode) -> None:
        await self._inner.on_interrupt(room_id, mode)

    async def on_message(self, msg, tools, history, participants_msg, contacts_msg, **kwargs):
        # Turns arrive through on_event below; this only serves callers that
        # drive run_judged_turn directly.
        await self._inner.on_message(msg, tools, history, participants_msg, contacts_msg, **kwargs)

    # -- lifecycle -------------------------------------------------------------

    async def on_started(self, agent_name: str, agent_description: str) -> None:
        await super().on_started(agent_name, agent_description)
        if self.platform is not None:
            # The runtime injects the platform connection on the wrapper only.
            setattr(self._inner, "platform", self.platform)  # noqa: B010
        arm_adapter(self._inner)
        await self._inner.on_started(agent_name, agent_description)
        logger.info(
            "cognee memory active: dataset=%s server=%s",
            self._settings.dataset,
            self._settings.base_url,
        )
        if not self._settings.api_key and _is_local_url(self._settings.base_url):
            logger.warning(
                "cognee memory: no COGNEE_API_KEY set for the local server %s; Cognee "
                ">= 1.2.2 requires one even on localhost, so memory calls will be "
                "rejected (401) and the agent will run without memory",
                self._settings.base_url,
            )

    async def on_event(self, inp) -> None:
        msg = inp.msg
        content = msg.content or ""
        if (
            msg.message_type not in _MEMORY_MESSAGE_TYPES
            # Band's own notices (contact requests), not conversation.
            or msg.sender_type == SYNTHETIC_SENDER_TYPE
            # Commands are parsed from the raw content by the inner adapter.
            or _is_command(content)
            or not content.strip()
        ):
            await self._inner.on_event(inp)
            return

        room_id = inp.room_id
        session_id = self._settings.session_id_for_room(room_id)
        self._active_rooms.add(room_id)
        self._flush_deferred(room_id)

        block = await self._recall_block(content, session_id)
        capture = _CaptureTools(inp.tools)
        forwarded = dataclasses.replace(
            inp,
            tools=capture,
            # Message first, memory after it in a delimited block, so the
            # sender-attributed text an adapter renders starts with what the
            # sender actually wrote.
            msg=dataclasses.replace(msg, content=f"{content}\n\n{block}") if block else msg,
        )
        question = f"{msg.sender_name}: {content}" if msg.sender_name else content
        try:
            await self._inner.on_event(forwarded)
        finally:
            self._capture_turn(room_id, question, capture, session_id)

    async def on_cleanup(self, room_id: str) -> None:
        try:
            await self._inner.on_cleanup(room_id)
        finally:
            self._active_rooms.discard(room_id)
            self._flush_deferred(room_id)
            await self._drain_pending(room_id)
            await self._improve(self._settings.session_id_for_room(room_id))

    async def cleanup_all(self) -> None:
        try:
            if self._inner._disposal.builtin_started:
                await dispose_framework(self._inner)
        finally:
            for room_id in list(self._deferred):
                self._flush_deferred(room_id)
            await self._drain_pending()
            for room_id in sorted(self._active_rooms):
                await self._improve(self._settings.session_id_for_room(room_id))
            self._active_rooms.clear()

    # -- memory plumbing -----------------------------------------------------------

    async def _recall_block(self, question: str, session_id: str) -> str:
        if not question.strip():
            return ""
        try:
            results = await asyncio.to_thread(self._client.recall, question, session_id=session_id)
        except Exception:
            logger.exception("cognee recall failed; continuing without memory")
            return ""
        if results == UNREACHABLE:
            logger.warning("cognee server unreachable; continuing without memory")
            return ""
        if isinstance(results, dict):  # error envelope from a reachable server
            logger.warning("cognee recall error: %s", results.get("error"))
            return ""
        return render_memory_block(results, question, self._settings.max_memory_chars)

    def _capture_turn(self, room_id: str, question: str, capture: _CaptureTools, session_id: str):
        """Store the turn's QA now, or once a detached turn has really ended."""
        if not capture.detached:
            self._store_turn(room_id, question, capture, session_id)
            return
        task = capture.turn_task
        if task is not None and not task.done():
            # Codex / ClaudeSDK: the worker the adapter registered keeps the
            # capture proxy, so the late reply lands in ``capture.sent``.
            task.add_done_callback(
                lambda _t: self._store_turn(room_id, question, capture, session_id)
            )
            return
        if task is not None:
            self._store_turn(room_id, question, capture, session_id)
            return
        # Cursor / OpenCode give no finish signal: store whatever was sent by
        # the room's next turn or its cleanup.
        self._deferred.setdefault(room_id, []).append((question, capture, session_id))

    def _flush_deferred(self, room_id: str) -> None:
        for question, capture, session_id in self._deferred.pop(room_id, ()):
            self._store_turn(room_id, question, capture, session_id)

    def _store_turn(self, room_id: str, question: str, capture: _CaptureTools, session_id: str):
        answer = "\n\n".join(capture.sent)
        if not answer.strip():
            # No reply (band_no_reply, an adapter error, an unanswered
            # approval): nothing worth turning into memory.
            return
        self._fire_and_forget(
            room_id, self._client.store_qa, question, answer, session_id=session_id
        )

    async def _improve(self, session_id: str) -> None:
        try:
            result = await asyncio.to_thread(self._client.improve, session_id)
            if not result.get("ok"):
                logger.warning("cognee improve failed for %s: %s", session_id, result)
        except Exception:
            logger.exception("cognee improve failed for %s", session_id)

    def _fire_and_forget(self, room_id: str, fn, *args, **kwargs) -> None:
        async def runner():
            try:
                await asyncio.to_thread(fn, *args, **kwargs)
            except Exception:
                logger.exception("cognee background store failed")

        task = asyncio.get_running_loop().create_task(runner())
        room_tasks = self._pending_tasks.setdefault(room_id, set())
        room_tasks.add(task)
        task.add_done_callback(room_tasks.discard)

    async def _drain_pending(self, room_id: str | None = None) -> None:
        """Let in-flight QA stores land before improve bridges them.

        With ``room_id`` only that room's writes are awaited; otherwise all.
        """
        if room_id is None:
            pending = [t for tasks in self._pending_tasks.values() for t in tasks]
        else:
            pending = list(self._pending_tasks.get(room_id, ()))
        pending = [t for t in pending if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if room_id is not None:
            self._pending_tasks.pop(room_id, None)
