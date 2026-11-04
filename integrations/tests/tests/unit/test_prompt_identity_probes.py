"""Prompt session lookup must not spend its recall budget on identity HTTP calls."""

from unittest.mock import Mock

import pytest


def test_session_lookup_does_not_probe_identity(suite, hook_module, monkeypatch):
    lookup = hook_module(suite, "session-context-lookup.py")
    pc = __import__("_plugin_common")
    monkeypatch.setattr(pc, "resolve_cognee_session_id", lambda key: "session-123")
    request = Mock(side_effect=AssertionError("identity HTTP call on prompt path"))
    monkeypatch.setattr(pc, "_json_http_request", request)

    assert lookup._load_session_id() == "session-123"
    request.assert_not_called()


@pytest.mark.parametrize("connection_has_identity", [True, False])
def test_other_callers_still_resolve_identity(
    suite, isolated_modules, monkeypatch, connection_has_identity
):
    pc = isolated_modules(suite, "_plugin_common")
    # The OSS chain: /users/me is the last resort on a server that is not a
    # cloud tenant, so the test pins a local URL whatever the shell carries.
    monkeypatch.setattr(pc, "_local_api_url", lambda: "http://localhost:8011")
    # A registered launch: the first lookup names its connection; when that
    # yields no user, any connection of the key is asked, then /users/me.
    monkeypatch.setattr(pc, "resolve_conn_uuid", lambda key: "conn_abc")
    connection = {"agent": {"user_id": "user-123"}} if connection_has_identity else {}
    request = Mock(side_effect=[connection, {}, {"id": "user-123"}])
    monkeypatch.setattr(pc, "_json_http_request", request)

    assert pc.load_resolved()["user_id"] == "user-123"
    assert request.call_count == (1 if connection_has_identity else 3)
    assert request.call_args_list[0].args[0].startswith("/api/v1/agents/connections/me?")
    if not connection_has_identity:
        assert request.call_args_list[1].args[0] == "/api/v1/agents/connections/me"
        assert request.call_args_list[2].args[0] == "/api/v1/users/me"
    assert request.call_args_list[0].kwargs["timeout"] == (
        15.0 if suite.name == "claude-code" else 10.0
    )


def test_identity_falls_back_to_any_connection_of_the_key(suite, isolated_modules, monkeypatch):
    """A session registered on another tenant has no connection under its name
    on this one (404), but the key's user is the same on every connection it
    holds, so the unnamed lookup answers; /users/me is not asked on a tenant."""
    import urllib.error

    pc = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(pc, "resolve_conn_uuid", lambda key: "conn_from_prod")
    monkeypatch.setattr(pc, "_local_api_url", lambda: "https://tenant-abc.stg-aws.cognee.ai")
    asked = []

    def request(path, *args, **kwargs):
        asked.append(path)
        if "agent_session_name=" in path:
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
        if path.endswith("/agents/connections/me"):
            return {"agent": {"id": "conn_other", "user_id": "user-123", "status": "active"}}
        raise AssertionError(f"unexpected {path}")

    monkeypatch.setattr(pc, "_json_http_request", request)
    assert pc.load_resolved()["user_id"] == "user-123"
    assert [p.split("?")[0] for p in asked] == ["/api/v1/agents/connections/me"] * 2
