"""Shared fixtures. Fakes the cognee HTTP client so tests are deterministic and
need no real cognee server or LLM keys."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Keep the developer's .env (real keys, a dashboard token) out of the tests. Set
# before any test module imports the package, which is when the file is read.
os.environ["WIDGET_ENV_FILE"] = ""


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
        code_lookup=AsyncMock(return_value=[]),
        forget=AsyncMock(return_value=None),
        # The dashboard reads these too; without them the fake raises
        # AttributeError from inside an asyncio.gather, where the failure
        # surfaces far from its cause.
        graph_summary=AsyncMock(return_value={}),
        graph=AsyncMock(return_value={"nodes": [], "edges": []}),
    )


@pytest.fixture(autouse=True)
def fresh_rate_limits(monkeypatch):
    """Default limits with empty counters for every test.

    The server keeps one in-process limiter, so without this the chat requests of
    earlier tests would count against later ones.
    """
    from cognee_integration_web_widget import server
    from cognee_integration_web_widget.rate_limit import RateLimits

    monkeypatch.setattr(server, "limits", RateLimits())
    monkeypatch.setattr(server, "TRUSTED_PROXIES", None)
