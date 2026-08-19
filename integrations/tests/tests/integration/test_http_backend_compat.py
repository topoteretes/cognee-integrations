"""Compatibility with Cognee HTTP backends that expose only the data plane."""

from __future__ import annotations

import pytest

REGISTER = "/api/v1/agents/register"
UNREGISTER = "/api/v1/agents/unregister"


@pytest.fixture
def pc(suite, isolated_modules, mock_server, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    monkeypatch.setattr(common, "hook_log", lambda *a, **kw: None)
    return common


def test_backend_reachability_uses_health_instead_of_optional_docs(pc, mock_server):
    """A backend without API docs is still usable when its health route is ready."""
    mock_server.force_response("GET", "/docs", 404, {"detail": "Not Found"})

    assert pc._backend_reachable(mock_server.url) is True
    mock_server.assert_called("GET", "/health")
    mock_server.assert_not_called("GET", "/docs")


def _register(pc):
    return pc.register_agent_via_http(
        agent_session_name="compat-test",
        session_id="session-test",
        dataset_names=["agent_sessions"],
    )


def test_missing_agent_registration_route_is_best_effort(pc, mock_server):
    mock_server.force_response("POST", REGISTER, 404, {"detail": "Not Found"})

    assert _register(pc) == (True, {"lifecycle_supported": False})


def test_missing_agent_unregistration_route_is_best_effort(pc, mock_server):
    mock_server.force_response("POST", UNREGISTER, 404, {"detail": "Not Found"})

    assert pc.unregister_agent_via_http(agent_session_name="compat-test") == (True, 0)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_agent_lifecycle_http_errors_remain_failures(pc, mock_server, status):
    mock_server.force_response("POST", REGISTER, status, {"detail": "rejected"})
    mock_server.force_response("POST", UNREGISTER, status, {"detail": "rejected"})

    assert _register(pc) == (False, {})
    assert pc.unregister_agent_via_http(agent_session_name="compat-test") == (False, 0)
