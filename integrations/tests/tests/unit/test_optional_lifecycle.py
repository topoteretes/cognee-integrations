"""An absent optional lifecycle API must not be reported as a bad API key."""

import asyncio
import urllib.error
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.parametrize("status", [200, 404, 401, 403, 500, 503])
def test_registration_statuses_remain_distinct(suite, hook_module, monkeypatch, status):
    start = hook_module(suite, "session-start.py")
    pc = __import__("_plugin_common")
    monkeypatch.setattr(start, "_resolve_single_principal_key", AsyncMock(return_value="test-key"))
    monkeypatch.setattr(start, "_user_id_via_api", AsyncMock(return_value="user"))
    request = Mock(return_value={"id": "connection"})
    if status != 200:
        request.side_effect = urllib.error.HTTPError("http://test", status, "test", None, None)
    monkeypatch.setattr(pc, "_json_http_request", request)
    call = start._ensure_agent_credentials_and_register(
        {"base_url": "http://test"}, "/project", "session", "connection", "host"
    )
    if status in (200, 404):
        result = asyncio.run(call)
        assert result[-1] is (status == 200)
        assert result[1] == "test-key"
    else:
        with pytest.raises(urllib.error.HTTPError) as exc:
            asyncio.run(call)
        assert exc.value.code == status
        assert start._status_from_error(exc.value) == status


@pytest.mark.parametrize("status", [404, 401, 403, 500])
def test_unregister_only_ignores_missing_route(suite, isolated_modules, monkeypatch, status):
    pc = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(
        pc,
        "_json_http_request",
        Mock(side_effect=urllib.error.HTTPError("http://test", status, "test", None, None)),
    )
    assert pc.unregister_agent_via_http(agent_session_name="session") == (status == 404, 0)


def test_transport_error_is_still_registration_failure(suite, isolated_modules, monkeypatch):
    pc = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(pc, "_json_http_request", Mock(side_effect=TimeoutError("offline")))
    ok, detail = pc.register_agent_via_http(agent_session_name="session")
    assert not ok and detail.get("lifecycle_supported") is not False


def test_reachability_uses_health(suite, isolated_modules, mock_server):
    pc = isolated_modules(suite, "_plugin_common")
    assert pc._backend_reachable(mock_server.url)
    mock_server.assert_called("GET", "/health")
    mock_server.assert_not_called("GET", "/docs")
