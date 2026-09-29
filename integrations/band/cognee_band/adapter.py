"""Cognee memory wrapper for any Band SDK adapter.

``CogneeMemoryAdapter`` decorates a constructed Band adapter (LangGraph,
Anthropic, CrewAI, ...) with shared Cognee memory:

  * before each room message reaches the inner adapter, relevant memory is
    recalled from Cognee and injected as a labeled block above the message
  * after the turn, the incoming message and the reply the adapter sent are
    stored as one QA pair in the Cognee session cache (fire-and-forget)
  * when a room is removed (``on_cleanup``) or the agent stops
    (``cleanup_all``), the session cache is bridged into the graph via
    ``/improve``

The wrapper deliberately imports nothing from ``band``: the Band ``Agent``
drives adapters through duck typing (``on_started`` / ``on_event`` /
``on_cleanup`` / ``cleanup_all`` and a ``setattr`` of ``_band_agent_id``), and
``AgentInput`` / ``PlatformMessage`` are dataclasses, so interception works via
attribute delegation and ``dataclasses.replace``. Memory I/O runs in worker
threads (the client is synchronous stdlib urllib) so Band's event loop never
blocks, and every memory failure is logged and swallowed — memory must never
break a turn.
"""

import asyncio
import dataclasses
import json
import logging

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


class CogneeMemoryAdapter:
    """Wrap a Band adapter with Cognee-backed memory.

    Usage::

        adapter = CogneeMemoryAdapter(AnthropicAdapter(...))
        agent = Agent.create(adapter=adapter, agent_id=..., api_key=...)
    """

    # Attributes that live on the wrapper itself. Everything else — including
    # the ``_band_agent_id`` the Band Agent sets before on_started — is
    # forwarded to the inner adapter so it behaves exactly as if unwrapped.
    _OWN_ATTRS = frozenset({"_inner", "_client", "_settings", "_active_rooms", "_pending_tasks"})

    def __init__(
        self, inner, *, settings: CogneeSettings | None = None, client: CogneeClient | None = None
    ):
        object.__setattr__(self, "_inner", inner)
        resolved = settings or CogneeSettings.resolve()
        object.__setattr__(self, "_settings", resolved)
        object.__setattr__(self, "_client", client or CogneeClient(resolved))
        object.__setattr__(self, "_active_rooms", set())
        object.__setattr__(self, "_pending_tasks", set())

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        if name in self._OWN_ATTRS:
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)

    # -- lifecycle -------------------------------------------------------------

    async def on_started(self, agent_name: str, agent_description: str) -> None:
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
            await self._improve(self._settings.session_id_for_room(room_id))

    async def cleanup_all(self) -> None:
        try:
            inner_cleanup = getattr(self._inner, "cleanup_all", None)
            if inner_cleanup is not None:
                await inner_cleanup()
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

    def _fire_and_forget(self, fn, *args, **kwargs) -> None:
        async def runner():
            try:
                await asyncio.to_thread(fn, *args, **kwargs)
            except Exception:
                logger.exception("cognee background store failed")

        task = asyncio.get_running_loop().create_task(runner())
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _drain_pending(self) -> None:
        """Let in-flight QA stores land before the final improve bridges them."""
        pending = [t for t in self._pending_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
