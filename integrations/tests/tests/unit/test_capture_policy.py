"""Capture controls protect both online and buffered automatic memory."""

import asyncio
import json
from unittest.mock import Mock

import pytest


@pytest.fixture
def policy(suite, isolated_modules):
    return isolated_modules(suite, "_capture_policy")


@pytest.mark.parametrize(
    "path", ["/project/.env", "C:\\repo\\.env.prod", ".ssh/id_rsa", "cert.pem", ".aws/credentials"]
)
def test_default_sensitive_paths_are_excluded(policy, path):
    assert not policy.allow_tool("Read", {"file_path": path})
    assert policy.allow_tool("Read", {"file_path": "/project/main.py"})


def test_tool_allowlist_and_extra_paths(policy, monkeypatch):
    monkeypatch.setenv("COGNEE_CAPTURE_TOOLS", "Grep|Glob")
    assert not policy.allow_tool("Read", {"path": "main.py"})
    assert policy.allow_tool("Grep", {"path": "main.py"})
    monkeypatch.setenv("COGNEE_CAPTURE_DENY_PATHS", '["*.private"]')
    assert not policy.allow_tool("Grep", {"path": "payments.private"})


@pytest.mark.parametrize(
    "text,secret",
    [
        ("password=supersecret", "supersecret"),
        ('{"api_key": "hidden-value"}', "hidden-value"),
        ("Authorization: Bearer abc.secret.value", "abc.secret.value"),
        ("postgresql://user:password@host/db", "user:password"),
        (
            "-----BEGIN PRIVATE KEY-----\nPRIVATE_CONTENT\n-----END PRIVATE KEY-----",
            "PRIVATE_CONTENT",
        ),
        ("ghp_012345678901234567890123456789", "012345678901234567890123456789"),
    ],
)
def test_secret_formats_are_scrubbed(policy, text, secret):
    assert secret not in policy.redact(text)
    assert "[redacted:" in policy.redact(text)


def test_nested_keys_and_custom_patterns(policy, monkeypatch):
    assert policy.redact({"params": {"access_token": "private-value"}}) == {
        "params": {"access_token": "[redacted:credential]"}
    }
    monkeypatch.setenv("COGNEE_CAPTURE_REDACT_PATTERNS", '["customer-[0-9]+"]')
    assert policy.redact("customer-123") == "[redacted:custom]"
    monkeypatch.setenv("COGNEE_CAPTURE_REDACT_PATTERNS", '["["]')
    with pytest.raises(Exception):
        policy.redact("secret-bearing input")


def test_capture_disabled_does_not_touch_session_state(suite, hook_module, monkeypatch):
    prompt = hook_module(suite, "store-user-prompt.py")
    store = hook_module(suite, "store-to-session.py")
    monkeypatch.setenv("COGNEE_CAPTURE", "false")
    for module in (prompt, store):
        monkeypatch.setattr(
            module, "_load_session", Mock(side_effect=AssertionError("capture ran"))
        )
    asyncio.run(prompt._store("a sensitive prompt", {}))
    asyncio.run(store._store_tool_call({"tool_name": "Read", "tool_input": {"path": "normal.py"}}))
    asyncio.run(store._store_assistant_stop({"assistant_message": "a sensitive answer"}))


def test_filtered_trace_never_reaches_session_or_buffer(suite, hook_module, monkeypatch):
    store = hook_module(suite, "store-to-session.py")
    monkeypatch.setattr(store, "_load_session", Mock(side_effect=AssertionError("capture ran")))
    asyncio.run(
        store._store_tool_call(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "/repo/.env"},
                "tool_response": "password=supersecret",
            }
        )
    )


def test_online_and_buffered_trace_receive_redacted_content(suite, hook_module, monkeypatch):
    for online in (False, True):
        store = hook_module(suite, "store-to-session.py")
        monkeypatch.setattr(store, "_load_session", lambda: ("session", "dataset", "user"))
        monkeypatch.setattr(store, "load_config", lambda: {})
        monkeypatch.setattr(
            store,
            "resolve_runtime_mode",
            lambda: {"mode": "http", "base_url": "http://example.test"},
        )
        monkeypatch.setattr(store, "server_usable", lambda url: online)
        writes = []
        monkeypatch.setattr(
            store, "append_warmup_entry", lambda *args, **kwargs: writes.append(args[2])
        )
        monkeypatch.setattr(
            store, "remember_entry_via_http", lambda *args, **kwargs: writes.append(args[2])
        )
        asyncio.run(
            store._store_tool_call(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo password=supersecret"},
                    "tool_response": "Bearer private.token.value",
                }
            )
        )
        assert len(writes) == 1
        serialized = json.dumps(writes[0])
        assert "supersecret" not in serialized and "private.token.value" not in serialized
