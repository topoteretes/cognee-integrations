"""Record every Deep Agents step into cognee, and recall what cognee knows.

``CogneeTraceMiddleware`` hooks Deep Agents' middleware points:

- ``abefore_agent`` recalls what cognee knows about the request — from the
  graph and from earlier sessions of the same thread — and adds it to the
  context as a system message, including lessons distilled by ``improve()``.
- ``awrap_tool_call`` stores every tool call (planning via ``write_todos``,
  sub-agent hand-offs via ``task``, file operations, custom tools) as a
  ``TraceEntry`` in the session cache, keyed by the LangGraph ``thread_id``.
- ``aafter_agent`` stores the turn's request and final answer as a ``QAEntry``.

Nothing cognee does may break the agent: every cognee call is wrapped, and a
failed write is logged and dropped while the tool result passes through
unchanged. Trace writes go to the fast session cache — no LLM call joins the
agent loop; ``improve()`` bridges the session into the graph later, on demand
through ``persist_thread`` or through cognee's own background debounce.

``with_cognee_tracing`` adds the middleware to each ``SubAgent`` spec so
declarative sub-agents record into the same session, labelled
``subagent:<name>/<tool>`` (middleware passed to ``create_deep_agent`` is not
inherited by them).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from deepagents import SubAgent
from langchain.agents.middleware import AgentMiddleware

logger = logging.getLogger(__name__)

# Tool results can carry whole files; only the tail after this many characters
# is dropped from the stored trace (the agent itself still sees the full result).
DEFAULT_MAX_RESULT_CHARS = 4000


def _thread_id(runtime: Any) -> str | None:
    """The LangGraph ``thread_id`` of this run, or ``None`` outside a thread."""
    try:
        from langgraph.config import get_config

        configurable = (get_config() or {}).get("configurable") or {}
        thread_id = configurable.get("thread_id")
        return str(thread_id) if thread_id else None
    except Exception:  # noqa: BLE001 - a missing thread must never break the agent
        return None


def _last_message_text(state: dict, kind: type) -> str:
    for message in reversed(state.get("messages") or []):
        if isinstance(message, kind):
            content = message.content
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            return str(content)
    return ""


class CogneeTraceMiddleware(AgentMiddleware):
    """Store every agent step in cognee's session cache and recall before the run.

    Args:
        dataset_name: cognee dataset the traces and Q&A land in.
        session_id: Fixed session id. By default the LangGraph ``thread_id`` is
            used, falling back to a fresh id per process run.
        redact: Optional hook applied to tool arguments and results, and to the
            recalled memory, before anything reaches cognee.
        exclude_tools: Tool names whose calls are not recorded at all.
        max_result_chars: Tool results are truncated to this many characters
            before storage.
        recall_top_k: How much memory ``abefore_agent`` recalls.
    """

    def __init__(
        self,
        dataset_name: str = "main_dataset",
        *,
        session_id: str | None = None,
        redact: Callable[[str], str] | None = None,
        exclude_tools: tuple[str, ...] = (),
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
        recall_top_k: int = 10,
    ):
        self.dataset_name = dataset_name
        self.session_id = session_id
        self.redact = redact
        self.exclude_tools = tuple(exclude_tools)
        self.max_result_chars = max_result_chars
        self.recall_top_k = recall_top_k
        self._run_session_id: str | None = None

    def _session(self, runtime: Any) -> str:
        if self.session_id:
            return self.session_id
        thread = _thread_id(runtime)
        if thread:
            return thread
        if not self._run_session_id:
            self._run_session_id = uuid.uuid4().hex
        return self._run_session_id

    def _redact(self, text: str) -> str:
        return self.redact(text) if self.redact else text

    def _redact_args(self, args: dict) -> dict:
        if self.redact:
            return {
                key: self._redact(value) if isinstance(value, str) else value
                for key, value in args.items()
            }
        return args

    def _truncate(self, text: str) -> str:
        if self.max_result_chars and len(text) > self.max_result_chars:
            return text[: self.max_result_chars] + f"… [{len(text) - self.max_result_chars} chars dropped]"
        return text

    async def abefore_agent(self, state: dict, runtime: Any) -> dict | None:
        """Recall what cognee knows about the request; add it as a system message."""
        try:
            from langchain_core.messages import HumanMessage, SystemMessage

            import cognee

            question = _last_message_text(state, HumanMessage)
            if not question:
                return None
            entries = await cognee.recall(
                self._redact(question),
                datasets=[self.dataset_name],
                session_id=self._session(runtime),
                top_k=self.recall_top_k,
            )
            recalled = "\n\n".join(str(getattr(entry, "text", "") or "") for entry in entries).strip()
            if not recalled:
                return None
            return {"messages": [SystemMessage(content=f"Memory relevant to this task:\n\n{recalled}")]}
        except Exception as exc:  # noqa: BLE001 - memory must never break the agent
            logger.warning("cognee recall before agent failed; continuing without it: %s", exc)
            return None

    async def awrap_tool_call(self, request: Any, handler: Callable) -> Any:
        """Run the tool, then store the call as a ``TraceEntry``."""
        result = await handler(request)
        try:
            call = request.call
            tool_name = str(call.get("name", ""))
            if tool_name and tool_name not in self.exclude_tools:
                await self._remember_trace(tool_name, call.get("args") or {}, result)
        except Exception as exc:  # noqa: BLE001 - memory must never break the agent
            logger.warning("cognee trace write for %s failed: %s", call.get("name", "?"), exc)
        return result

    async def _remember_trace(self, tool_name: str, args: dict, result: Any) -> None:
        import cognee

        from cognee.memory.entries import TraceEntry

        content = getattr(result, "content", result)
        if isinstance(content, list):
            content = " ".join(str(part) for part in content)
        text = self._truncate(self._redact(str(content or "")))
        status = "error" if bool(getattr(result, "status", "success") == "error") else "success"
        error_message = ""
        if status == "error":
            error_message = text
        entry = TraceEntry(
            origin_function=tool_name,
            status=status,
            method_params=self._redact_args(args),
            method_return_value=None if status == "error" else text,
            error_message=error_message,
        )
        session = self._session(None)
        await cognee.remember(entry, dataset_name=self.dataset_name, session_id=session)

    async def aafter_agent(self, state: dict, runtime: Any) -> None:
        """Store the turn's request and final answer as a ``QAEntry``."""
        try:
            from langchain_core.messages import AIMessage, HumanMessage

            import cognee

            from cognee.memory.entries import QAEntry

            question = _last_message_text(state, HumanMessage)
            answer = _last_message_text(state, AIMessage)
            if not question or not answer:
                return
            entry = QAEntry(
                question=self._redact(question),
                answer=self._truncate(self._redact(answer)),
            )
            await cognee.remember(entry, dataset_name=self.dataset_name, session_id=self._session(runtime))
        except Exception as exc:  # noqa: BLE001 - memory must never break the agent
            logger.warning("cognee QA write failed: %s", exc)


def with_cognee_tracing(
    subagents: list[SubAgent],
    middleware: CogneeTraceMiddleware | None = None,
) -> list[SubAgent]:
    """Copy each ``SubAgent`` spec with ``middleware`` (default: a fresh one) added.

    Middleware passed to ``create_deep_agent`` is not inherited by declarative
    sub-agents, so their steps would otherwise stay out of cognee. The specs are
    shallow-copied, never mutated.
    """
    traced: list[SubAgent] = []
    for spec in subagents:
        if isinstance(spec, dict):
            copy = dict(spec)
            copy["middleware"] = [*copy.get("middleware", []), middleware or CogneeTraceMiddleware()]
        else:
            copy = spec
            existing = list(getattr(spec, "middleware", None) or [])
            object.__setattr__(
                spec,
                "middleware",
                [*existing, middleware or CogneeTraceMiddleware()],
            )
        traced.append(copy)
    return traced


async def persist_thread(thread_id: str, dataset_name: str = "main_dataset") -> None:
    """Bridge one thread's session cache into the permanent graph, explicitly.

    Runs ``cognee.improve`` for the thread's session: agent traces become graph
    nodes and edges, and the distilled lessons surface in later recalls. Safe to
    call at any time; cognee's own background bridge covers the rest.
    """
    import cognee

    await cognee.improve(dataset_name, session_ids=[thread_id])
