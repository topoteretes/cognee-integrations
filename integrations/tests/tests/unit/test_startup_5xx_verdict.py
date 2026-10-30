"""A 5xx from one startup call must not paint a working cloud server red.

Cognee's ``/health`` answers 503 when one of the server's own component checks
fails, while the API keeps serving. The cloud bootstrap used to record that as
``server_error``, so every new session showed ``✕ (server_error)`` until the
cloud worker's next authenticated check flipped it back to ready.

Contract: before recording a 5xx-derived ``server_error``, the bootstrap asks
the authenticated endpoint once more; a ``ready`` answer wins, anything else
keeps ``server_error``.

claude-code only: the other hosts have no cloud bootstrap worker.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

URL = "https://tenant.example.cognee.ai"


@pytest.fixture
def _supported(suite):
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: no cloud bootstrap worker")


@pytest.fixture
def session_start(suite, _supported, hook_module, monkeypatch):
    module = hook_module(suite, "session-start.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(module, "_reexec_into_venv", lambda: None)
    monkeypatch.setattr(module, "is_cloud_mode", lambda config: True)

    async def health_503(config):
        raise RuntimeError("backend health check failed (503: not ready)")

    async def registered(*a, **k):
        return "agent-id", "agent-key", "agent", True

    monkeypatch.setattr(module, "ensure_cognee_ready", health_503)
    monkeypatch.setattr(module, "_ensure_agent_credentials_and_register", registered)
    # The dataset is already resolved, so the bootstrap skips creating it.
    monkeypatch.setattr(
        sys.modules["_plugin_common"], "resolve_active_dataset_ids", lambda key: ("ds-id",)
    )
    import _code_graph
    import _project_memory

    monkeypatch.setattr(_project_memory, "prepare", lambda *a, **k: {})
    monkeypatch.setattr(_code_graph, "autoindex_on_session_start", lambda *a, **k: None)
    return module


def _run(module, monkeypatch, *, liveness: str) -> tuple[list, list]:
    written: list = []
    probed: list = []
    monkeypatch.setattr(
        module, "write_connection_state", lambda state, url="", **kw: written.append(state)
    )

    def fake_liveness(url, api_key="", timeout=1.5):
        probed.append((url, api_key))
        return liveness

    monkeypatch.setattr(module, "authed_liveness", fake_liveness)
    monkeypatch.setattr(module, "probe_health", lambda *a, **k: "unknown")
    asyncio.run(
        module._run_heavy(
            {"base_url": URL, "api_key": "user-key"},
            "",
            "sid",
            "agent",
            "key",
            "agent_sessions",
            managed_endpoint=True,
            boot_timeout=1,
        )
    )
    return written, probed


def test_a_503_health_with_a_working_api_records_ready(session_start, monkeypatch):
    written, probed = _run(session_start, monkeypatch, liveness="ready")
    assert written == ["ready"]
    assert probed == [(URL, "agent-key")]


@pytest.mark.parametrize("liveness", ["server_error", "slow", "unknown"])
def test_a_5xx_the_authed_probe_does_not_clear_stays_server_error(
    session_start, monkeypatch, liveness
):
    written, _ = _run(session_start, monkeypatch, liveness=liveness)
    assert written == ["server_error"]
