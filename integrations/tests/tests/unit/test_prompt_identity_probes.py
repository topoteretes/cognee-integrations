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
    connection = {"agent": {"user_id": "user-123"}} if connection_has_identity else {}
    request = Mock(side_effect=[connection, {"id": "user-123"}])
    monkeypatch.setattr(pc, "_json_http_request", request)

    assert pc.load_resolved()["user_id"] == "user-123"
    assert request.call_count == (1 if connection_has_identity else 2)
