"""Shared fixtures. Fakes the cognee HTTP client so tests are deterministic and
need no real cognee server or LLM keys."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def fake_client():
    """A stand-in for :class:`CogneeHttpClient` with AsyncMock methods.

    Injected straight into ``ChatMemoryAdapter(client=...)`` — no network, no
    cognee server, no keys. The three methods mirror ``CogneeHttpClient``:
    ``remember`` / ``recall`` / ``forget``.
    """
    return SimpleNamespace(
        remember=AsyncMock(return_value=None),
        recall=AsyncMock(return_value=[]),
        forget=AsyncMock(return_value=None),
        # The dashboard reads these too; without them the fake raises
        # AttributeError from inside an asyncio.gather, where the failure
        # surfaces far from its cause.
        graph_summary=AsyncMock(return_value={}),
        graph=AsyncMock(return_value={"nodes": [], "edges": []}),
    )
