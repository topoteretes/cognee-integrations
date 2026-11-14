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


def test_session_start_registers_as_the_owner_key_and_stores_nothing(
    suite, run_hook, payloads, mock_server, temp_home
):
    """The mock listens on this machine, so it is a local server: the session
    registers as its owner key, and no key is stored anywhere."""
    owner = mock_server.identity.seed_owner_key("default_user@example.com")
    result = run_hook(
        suite,
        "session-start.py",
        stdin=payloads.session_start(),
        service_url=mock_server.url,
        api_key=DEFAULT_TEST_API_KEY,
    )
    assert result.returncode == 0, result.stderr
    registered = [c for c in mock_server.calls if c["path"] == "/api/v1/agents/register"]
    assert registered and registered[-1]["headers"].get("X-Api-Key") == owner
    assert not any("/integrations/plugins/" in c["path"] for c in mock_server.calls)
    assert not (temp_home / ".cognee-plugin" / "api_key.json").exists()
    assert not (temp_home / ".cognee-plugin" / suite.state_subdir / "agent_key.json").exists()


# ── the Cloud key never goes to a local server (finding 4) ─────────────────


def test_a_local_url_gets_the_owner_key_not_the_option_key(
    suite, isolated_modules, mock_server, monkeypatch
):
    pc = isolated_modules(suite, "_plugin_common")
    owner = mock_server.identity.seed_owner_key("default_user@example.com")
    monkeypatch.setenv("COGNEE_API_KEY", "ck_cloud_option")
    assert pc._api_key_with_source(mock_server.url) == (owner, "local_owner_key")
    assert pc._api_key_with_source("https://tenant.cognee.ai") == (
        "ck_cloud_option",
        "env_api_key",
    )


def test_session_start_sends_the_local_server_its_owner_key(suite, run_hook, payloads, mock_server):
    """Key option set, URL pointing at this machine: the option never leaves."""
    owner = mock_server.identity.seed_owner_key("default_user@example.com")
    result = run_hook(
        suite,
        "session-start.py",
        stdin=payloads.session_start(),
        service_url=mock_server.url,
        api_key="ck_cloud_option",
    )
    assert result.returncode == 0, result.stderr
    sent = [c["headers"].get("X-Api-Key") for c in mock_server.calls]
    assert "ck_cloud_option" not in sent
    registered = [c for c in mock_server.calls if c["path"] == "/api/v1/agents/register"]
    assert registered and registered[-1]["headers"].get("X-Api-Key") == owner


# ── a listed key that cannot work is replaced (finding 7) ──────────────────


def _register_key(mock_server) -> str:
    registered = [c for c in mock_server.calls if c["path"] == "/api/v1/agents/register"]
    assert registered, [c["path"] for c in mock_server.calls]
    return registered[-1]["headers"].get("X-Api-Key")


def _minted(mock_server) -> list:
    return [
        c
        for c in mock_server.calls
        if c["path"] == "/api/v1/auth/api-keys" and c["method"] == "POST"
    ]


def test_a_masked_listed_key_counts_as_none(suite, isolated_modules, mock_server, monkeypatch):
    pc = isolated_modules(suite, "_plugin_common")
    mock_server.identity.seed_user("default_user@example.com")
    mock_server.identity.user_api_keys["default_user@example.com"] = [{"key": "************"}]
    assert pc.is_masked_key("************")
    assert pc._local_owner_key(mock_server.url) == ""


def test_session_start_mints_a_key_when_the_listed_one_is_masked(
    suite, run_hook, payloads, mock_server
):
    mock_server.identity.seed_user("default_user@example.com")
    mock_server.identity.user_api_keys["default_user@example.com"] = [{"key": "************"}]
    result = run_hook(
        suite, "session-start.py", stdin=payloads.session_start(), service_url=mock_server.url
    )
    assert result.returncode == 0, result.stderr
    assert _minted(mock_server)
    key = _register_key(mock_server)
    assert key and key != "************"


def test_session_start_mints_a_key_when_the_listed_one_is_rejected(
    suite, run_hook, payloads, mock_server
):
    """A key stored hashed by an earlier HASH_API_KEY=true server lists fine but
    fails the authenticated check: a fresh owner key is minted and used."""
    stale = mock_server.identity.seed_owner_key("default_user@example.com")
    mock_server.identity.invalidate_key(stale)
    result = run_hook(
        suite, "session-start.py", stdin=payloads.session_start(), service_url=mock_server.url
    )
    assert result.returncode == 0, result.stderr
    assert _minted(mock_server)
    key = _register_key(mock_server)
    assert key and key != stale


# ── the owner-key lookup: no cached failures, short on blocking hooks (13) ──


def test_a_failed_lookup_is_retried_after_the_backoff(
    suite, isolated_modules, mock_server, monkeypatch
):
    pc = isolated_modules(suite, "_plugin_common")
    url = mock_server.url

    def logins():
        return len([c for c in mock_server.calls if c["path"] == "/api/v1/auth/login"])

    mock_server.identity.reject_login = True
    assert pc._local_owner_key(url) == ""
    first = logins()
    assert pc._local_owner_key(url) == ""  # inside the backoff: no new attempt
    assert logins() == first

    mock_server.identity.reject_login = False
    owner = mock_server.identity.seed_owner_key("default_user@example.com")
    clock = pc.time.monotonic() + pc._LOCAL_OWNER_RETRY_SECONDS + 1
    monkeypatch.setattr(pc.time, "monotonic", lambda: clock)
    assert pc._local_owner_key(url) == owner
    assert logins() == first + 1


def test_an_empty_key_list_returns_nothing_and_is_not_kept(suite, isolated_modules, mock_server):
    pc = isolated_modules(suite, "_plugin_common")
    mock_server.identity.seed_user("default_user@example.com")
    assert pc._local_owner_key(mock_server.url) == ""
    assert not pc._LOCAL_OWNER_KEYS.get(pc._normalize_service_url(mock_server.url))


def test_the_lookup_is_logged_with_its_duration(suite, isolated_modules, mock_server, temp_home):
    pc = isolated_modules(suite, "_plugin_common")
    owner = mock_server.identity.seed_owner_key("default_user@example.com")
    assert pc._local_owner_key(mock_server.url) == owner
    lines = [json.loads(line) for line in pc._HOOK_LOG.read_text().splitlines() if line.strip()]
    timed = [ln for ln in lines if ln.get("event") == "local_owner_key_lookup"]
    assert timed and isinstance(timed[-1]["detail"]["local_owner_key_lookup_ms"], float)
    assert owner not in pc._HOOK_LOG.read_text()


def test_a_blocking_hook_gives_up_after_one_and_a_half_seconds(
    suite, isolated_modules, monkeypatch
):
    """A server that accepts the connection but never answers."""
    import socket
    import time

    pc = isolated_modules(suite, "_plugin_common")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    try:
        monkeypatch.setattr(pc.sys, "argv", ["/plugin/scripts/session-context-lookup.py"])
        assert pc._owner_lookup_timeout() == 1.5
        started = time.monotonic()
        assert pc._local_owner_key(url) == ""
        elapsed = time.monotonic() - started
        assert 1.3 <= elapsed < 3.0, elapsed

        monkeypatch.setattr(pc.sys, "argv", ["/plugin/scripts/store-to-session.py"])
        assert pc._owner_lookup_timeout() == 5.0
    finally:
        listener.close()


# ── Bash-run commands without a key (finding 10's shared guard) ────────────


def test_require_shell_auth_stops_a_keyless_remote_command(suite, isolated_modules, capsys):
    pc = isolated_modules(suite, "_plugin_common")
    with pytest.raises(SystemExit) as stopped:
        pc.require_shell_auth("https://tenant.cognee.ai", "", "cognee-search")
    assert stopped.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("cognee-search: no API key.")
    assert "passes to the plugin's hooks only" in err
    assert "automatic recall and capture still work" in err


def test_require_shell_auth_lets_local_and_keyed_commands_run(suite, isolated_modules):
    pc = isolated_modules(suite, "_plugin_common")
    pc.require_shell_auth("http://localhost:8011", "", "cognee-search")
    pc.require_shell_auth("", "", "cognee-search")
    pc.require_shell_auth("https://tenant.cognee.ai", "ck_x", "cognee-search")
