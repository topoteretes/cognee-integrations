"""Step hooks: cheap local metadata, caller callbacks composed, nothing sent to cognee.

Browser Use's ``Agent.run(on_step_start=..., on_step_end=...)`` takes one
coroutine function per hook. The adapter composes the caller's hook with its own
observer: the caller's runs first, exceptions from the observer never reach the
agent, and nothing here calls cognee or an LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .sanitize import sanitize_url

StepHook = Callable[[Any], Awaitable[None]]


@dataclass
class StepObservation:
    step: int
    url: str = ""
    actions: list[str] = field(default_factory=list)


async def observe_agent_step(agent: Any) -> StepObservation:
    """Collect step number, sanitized URL and action names from an ``Agent``, best effort."""
    step = 0
    try:
        step = int(getattr(getattr(agent, "state", None), "n_steps", 0) or 0)
    except (TypeError, ValueError):
        step = 0
    url = ""
    session = getattr(agent, "browser_session", None)
    getter = getattr(session, "get_current_page_url", None)
    if callable(getter):
        try:
            raw = await getter()
            url = sanitize_url(str(raw or ""))
        except Exception:  # noqa: BLE001 - observation must never break the run
            url = ""
    actions: list[str] = []
    try:
        history = getattr(agent, "history", None)
        items = getattr(history, "history", None) or []
        if items:
            output = getattr(items[-1], "model_output", None)
            for action in getattr(output, "action", None) or []:
                dumped = (
                    action.model_dump(exclude_none=True) if hasattr(action, "model_dump") else {}
                )
                actions.extend(str(name) for name in dumped.keys())
    except Exception:  # noqa: BLE001
        actions = []
    return StepObservation(step=step, url=url, actions=actions)


def compose_hooks(caller: Optional[StepHook], ours: Optional[StepHook]) -> Optional[StepHook]:
    """Run the caller's hook, then ours; our failures are swallowed, the caller's propagate."""
    if caller is None and ours is None:
        return None

    async def combined(agent: Any) -> None:
        if caller is not None:
            await caller(agent)
        if ours is not None:
            try:
                await ours(agent)
            except Exception:  # noqa: BLE001 - memory observation never fails the run
                pass

    return combined
