"""Antigravity compatibility with data-plane-only Cognee HTTP servers."""

import pytest
from utils.suites import ANTIGRAVITY

_REGISTER = "/api/v1/agents/register"
_UNREGISTER = "/api/v1/agents/unregister"


@pytest.fixture
def common(isolated_modules, mock_server, monkeypatch):
    """Load only Antigravity's helper against the ephemeral HTTP backend."""
    module = isolated_modules(ANTIGRAVITY, "_plugin_common")
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    monkeypatch.setenv("COGNEE_API_KEY", "test-api-key")
    monkeypatch.setattr(module, "hook_log", lambda *args, **kwargs: None)
    return module


def test_backend_reachability_uses_the_data_plane_health_endpoint(common, mock_server):
    """A data-plane backend need not expose the interactive API documentation."""
    assert common._backend_reachable(mock_server.url) is True
    mock_server.assert_called("GET", "/health")
    mock_server.assert_not_called("GET", "/docs")


def test_registration_404_is_best_effort_for_a_data_plane_only_backend(common, mock_server):
    """Lifecycle registration is optional when only the Cognee data plane exists."""
    mock_server.force_response("POST", _REGISTER, 404, {"detail": "missing"})

    assert common.register_agent_via_http(agent_session_name="antigravity-test") == (
        False,
        {"lifecycle_supported": False},
    )


def test_unregistration_404_is_best_effort_for_a_data_plane_only_backend(common, mock_server):
    """Lifecycle teardown is optional when its server route is unavailable."""
    mock_server.force_response("POST", _UNREGISTER, 404, {"detail": "missing"})

    assert common.unregister_agent_via_http(agent_session_name="antigravity-test") == (True, 0)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_non_404_lifecycle_http_errors_remain_failures(common, mock_server, status):
    """Only a missing lifecycle route is optional; auth and server failures are real."""
    mock_server.force_response("POST", _REGISTER, status, {"detail": "error"})
    assert common.register_agent_via_http(agent_session_name="antigravity-test") == (
        False,
        {"status_code": status},
    )

    mock_server.force_response("POST", _UNREGISTER, status, {"detail": "error"})
    assert common.unregister_agent_via_http(agent_session_name="antigravity-test") == (False, 0)
