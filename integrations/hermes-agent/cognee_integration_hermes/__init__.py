"""Cognee memory provider plugin for Hermes Agent."""

from .provider import CogneeMemoryProvider

__all__ = ["CogneeMemoryProvider", "register", "transform_llm_output"]


def register(ctx) -> None:
    """Register the memory provider and the hook that draws its memory credit."""
    ctx.register_memory_provider(CogneeMemoryProvider())
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("transform_llm_output", transform_llm_output)


def transform_llm_output(response_text="", platform="", **_kwargs):
    """Draw the answer's ``> Cognee:`` credit for the chat's platform (None: unchanged)."""
    from .memory_credit import render_credit

    return render_credit(response_text, platform)
