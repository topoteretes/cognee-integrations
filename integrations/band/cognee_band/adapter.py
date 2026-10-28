"""Cognee memory wrapper for any Band SDK adapter.

``CogneeMemoryAdapter`` decorates a constructed Band adapter (LangGraph,
Anthropic, CrewAI, ...) with shared Cognee memory:

  * before each room message reaches the inner adapter, relevant memory is
    recalled from Cognee and injected as a labeled block above the message
  * after the turn, the incoming message and the reply the adapter sent are
    stored as one QA pair in the Cognee session cache (fire-and-forget)
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
import json
import logging
from typing import Any

from band.core.simple_adapter import SimpleAdapter
from band.runtime.startup import accept_adapter, arm_adapter, dispose_adapter, dispose_framework

from .client import UNREACHABLE, CogneeClient
from .config import CogneeSettings

logger = logging.getLogger("cognee_band")

# Only real conversation turns get recall + QA capture; tool_call/thought/task
# events pass through untouched.
_MEMORY_MESSAGE_TYPES = {"text"}

_CONTEXT_HEADER = (
    "Cognee memory — context recalled from shared memory, possibly relevant to the message below:"
)


def _render_result(item) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("text", "context", "content", "search_result", "answer"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(item)[:500]
    return str(item).strip()


def render_memory_block(results: list) -> str:
    """Format recall results as a labeled context block ('' when nothing usable)."""
    lines = [f"- {text}" for text in (_render_result(r) for r in results) if text]
    if not lines:
        return ""
    return _CONTEXT_HEADER + "\n" + "\n".join(lines)


class _CaptureTools:
    """Transparent proxy over Band's AgentTools that records sent replies."""

    def __init__(self, inner):
        self._inner = inner
        self.sent: list[str] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def send_message(self, content, mentions=None):
        if isinstance(content, str) and content.strip():
            self.sent.append(content)
        return await self._inner.send_message(content, mentions=mentions)


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

    async def on_event(self, inp) -> None:
        if inp.msg.message_type not in _MEMORY_MESSAGE_TYPES:
            await self._inner.on_event(inp)
            return

        question = inp.msg.content or ""
        session_id = self._settings.session_id_for_room(inp.room_id)
        self._active_rooms.add(inp.room_id)

        block = await self._recall_block(question, session_id)
        capture = _CaptureTools(inp.tools)
        forwarded = dataclasses.replace(
            inp,
            tools=capture,
            msg=(
                dataclasses.replace(inp.msg, content=f"{block}\n\n---\n\n{question}")
                if block
                else inp.msg
            ),
        )
        try:
            await self._inner.on_event(forwarded)
        finally:
            if question.strip():
                answer = "\n\n".join(capture.sent)
                self._fire_and_forget(
                    inp.room_id,
                    self._client.store_qa,
                    question,
                    answer,
                    session_id=session_id,
                )

    async def on_cleanup(self, room_id: str) -> None:
        try:
            await self._inner.on_cleanup(room_id)
        finally:
            self._active_rooms.discard(room_id)
            await self._drain_pending(room_id)
            await self._improve(self._settings.session_id_for_room(room_id))

    async def cleanup_all(self) -> None:
        try:
            if self._inner._disposal.builtin_started:
                await dispose_framework(self._inner)
        finally:
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
        return render_memory_block(results)

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
