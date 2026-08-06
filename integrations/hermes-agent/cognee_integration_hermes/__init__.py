"""Cognee memory provider plugin for Hermes Agent."""

from .provider import CogneeMemoryProvider

__all__ = ["CogneeMemoryProvider", "register"]


def register(ctx) -> None:
    """Pip entry-point registration hook for Hermes Agent."""
    ctx.register_memory_provider(CogneeMemoryProvider())
