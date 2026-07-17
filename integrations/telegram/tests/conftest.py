"""Shared fixtures. Fakes the cognee HTTP client so tests are deterministic and
need no real Telegram, cognee server, or LLM keys."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def mock_cognee(monkeypatch):
    """Replace the adapter's cognee HTTP client with AsyncMocks.

    Patches the client factory the adapter constructs by default, so
    ``CogneeMemoryAdapter()`` (no explicit client) picks up the fake — no
    network, no cognee server, no keys. The three methods mirror
    ``CogneeHttpClient``: ``remember`` / ``recall`` / ``forget``.
    """
    client = SimpleNamespace(
        remember=AsyncMock(return_value=None),
        recall=AsyncMock(return_value=[]),
        forget=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "cognee_integration_telegram.adapter.CogneeHttpClient",
        lambda *args, **kwargs: client,
    )
    return client


@pytest.fixture
def graph_result():
    """A recall result dict shaped like cognee's graph-completion entry."""

    def _make(text: str, source: str = "graph") -> dict:
        return {"text": text, "source": source}

    return _make


@pytest.fixture
def session_result():
    """A recall result dict shaped like a session-cache (QA) entry."""

    def _make(answer: str) -> dict:
        return {"answer": answer, "source": "session"}

    return _make
