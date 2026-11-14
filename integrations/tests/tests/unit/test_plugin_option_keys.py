"""Keys from the plugin options only, none on disk (SDK-1411, Claude Code).

The Claude Code plugin takes the user's Cognee Cloud key from its plugin
options, stores no key anywhere, and runs without a plugin identity. Against
its own local server it looks the owner key up from that server, once per
process.
"""

import json

import pytest
from utils.fixtures import DEFAULT_TEST_API_KEY


@pytest.fixture(autouse=True)
def _options_suite_only(suite):
    if not suite.keys_from_plugin_options:
        pytest.skip(f"{suite.name}: reads its keys from the environment")


def test_local_owner_key_is_looked_up_from_the_local_server(
    suite, isolated_modules, mock_server, monkeypatch
):
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    pc = isolated_modules(suite, "_plugin_common")
    created = "k-local-owner"
    mock_server.identity.seed_api_key(created)
    mock_server.identity.user_api_keys["default_user@example.com"] = [{"key": created}]

    assert pc.load_cached_api_key(mock_server.url) == created
    assert pc._api_key_with_source(mock_server.url) == (created, "local_owner_key")

    # Once per process: a second lookup costs no request.
    logins = len([c for c in mock_server.calls if c["path"] == "/api/v1/auth/login"])
    pc.load_cached_api_key(mock_server.url)
    assert len([c for c in mock_server.calls if c["path"] == "/api/v1/auth/login"]) == logins


def test_a_remote_server_has_no_fallback_key(suite, isolated_modules, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", "https://tenant.cognee.ai")
    pc = isolated_modules(suite, "_plugin_common")
    assert pc.load_cached_api_key("https://tenant.cognee.ai") == ""
    assert pc._api_key_with_source("https://tenant.cognee.ai") == ("", "missing")


def test_no_key_file_is_read_or_written(suite, isolated_modules, temp_home, monkeypatch):
    remote = "https://tenant.cognee.ai"
    for path in (
        temp_home / ".cognee-plugin" / "api_key.json",
        temp_home / ".cognee-plugin" / suite.state_subdir / "agent_key.json",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"api_key": "k-leftover", "base_url": remote}))
    monkeypatch.setenv("COGNEE_BASE_URL", remote)
    pc = isolated_modules(suite, "_plugin_common")

    assert pc.load_cached_api_key(remote) == ""
    assert pc.load_cached_agent_key(remote) == ""
    pc.save_cached_api_key(remote, "k-new")
    pc.save_cached_agent_key(remote, "k-agent", "agent-1", principal_key="k-new")
    for path in (
        temp_home / ".cognee-plugin" / "api_key.json",
        temp_home / ".cognee-plugin" / suite.state_subdir / "agent_key.json",
    ):
        assert "k-new" not in path.read_text() and "k-agent" not in path.read_text()


def test_plugin_identity_is_always_off(suite, isolated_modules, monkeypatch):
    monkeypatch.setenv("COGNEE_PLUGIN_IDENTITY", "true")
    pc = isolated_modules(suite, "_plugin_common")
    assert pc.plugin_identity_mode() == "disabled"
    assert pc.plugin_identity_mode({"plugin_identity": "true"}) == "disabled"


def test_session_start_registers_as_the_option_key_and_stores_nothing(
    suite, run_hook, payloads, mock_server, temp_home
):
    result = run_hook(
        suite,
        "session-start.py",
        stdin=payloads.session_start(),
        service_url=mock_server.url,
        api_key=DEFAULT_TEST_API_KEY,
    )
    assert result.returncode == 0, result.stderr
    registered = [c for c in mock_server.calls if c["path"] == "/api/v1/agents/register"]
    assert registered and registered[-1]["headers"].get("X-Api-Key") == DEFAULT_TEST_API_KEY
    assert not any("/integrations/plugins/" in c["path"] for c in mock_server.calls)
    assert not (temp_home / ".cognee-plugin" / "api_key.json").exists()
    assert not (temp_home / ".cognee-plugin" / suite.state_subdir / "agent_key.json").exists()
