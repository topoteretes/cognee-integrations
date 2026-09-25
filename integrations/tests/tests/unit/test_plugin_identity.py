"""Plugin identity provisioning — the server's per-plugin agent registration.

Cognee now provisions a dedicated agent sub-user + labeled API key per plugin
(POST /api/v1/integrations/plugins/{plugin_key}/provision) and expects agent
connections to self-declare their type at /agents/register. The client policy
under test (see session-start.py `_ensure_plugin_identity`):

  - registrations self-declare the suite's connection type, not generic "api"
  - a cached agent key wins outright and is never re-provisioned (the server
    ROTATES on every provision call — re-provisioning would revoke the key any
    other machine still holds)
  - identity mode ``auto`` (default) provisions only in service of shared
    agent memory and falls back to the principal when that cannot be wired;
    ``true`` is explicit and never falls back to the owner; ``false`` runs as
    the principal
  - provisioning is create-only: a server without that contract is
    "unsupported" — an error under ``true``, the principal under ``auto``
  - an auth-rejected agent key is blocked and never re-provisioned: an error
    under ``true``, a fallback to the principal under ``auto``
"""

from __future__ import annotations

import asyncio
import urllib.error

import pytest

#: Self-declared connection type per suite (KNOWN_AGENT_CONNECTION_TYPES).
CONNECTION_TYPE = {"claude-code": "claude_code", "codex": "codex", "antigravity": "antigravity"}

#: Provision route per suite (the plugin key is the suite name for both).
PROVISION_PATH = {
    name: f"/api/v1/integrations/plugins/{name}/provision" for name in CONNECTION_TYPE
}

PRINCIPAL_KEY = "test-api-key"


@pytest.fixture
def pc(suite, isolated_modules, mock_server, monkeypatch):
    """The suite's _plugin_common, pointed at the mock server.

    Env is set AFTER the isolated import: the loader scrubs COGNEE_* first.
    """
    module = isolated_modules(suite, "_plugin_common")
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    monkeypatch.setenv("COGNEE_API_KEY", PRINCIPAL_KEY)
    return module


# ── register: self-declared connection type ────────────────────────────────


def test_register_self_declares_connection_type(suite, pc, mock_server):
    ok, _body = pc.register_agent_via_http(agent_session_name="sess-a", session_id="s1")
    assert ok
    mock_server.assert_called("POST", "/api/v1/agents/register", type=CONNECTION_TYPE[suite.name])


def test_register_surfaces_auth_rejection(suite, pc, mock_server):
    mock_server.force_response("POST", "/api/v1/agents/register", 401, {"detail": "revoked"})
    ok, body = pc.register_agent_via_http(agent_session_name="sess-a")
    assert not ok
    assert body.get("auth_failed") is True


def test_register_transport_error_is_not_auth_failure(
    suite, pc, mock_server, closed_port_url, monkeypatch
):
    monkeypatch.setenv("COGNEE_BASE_URL", closed_port_url)
    ok, body = pc.register_agent_via_http(agent_session_name="sess-a", timeout=2.0)
    assert not ok
    assert not body.get("auth_failed")


# ── provision endpoint client ───────────────────────────────────────────────


def test_provision_normalizes_camel_case_response(suite, pc, mock_server):
    status, body = pc.provision_plugin_agent_via_http(principal_key=PRINCIPAL_KEY)
    assert status == "provisioned"
    assert body["api_key"].startswith("agentkey-")
    assert body["agent_id"]
    assert body["created"] is True
    entry = mock_server.assert_called("POST", PROVISION_PATH[suite.name])
    # Provisioning must authenticate as the PRINCIPAL, never the agent key.
    assert entry["headers"].get("X-Api-Key") == PRINCIPAL_KEY


def test_provision_unsupported_on_older_server(suite, pc, mock_server):
    mock_server.identity.plugin_provisioning = False
    status, body = pc.provision_plugin_agent_via_http(principal_key=PRINCIPAL_KEY)
    assert status == "unsupported"
    assert body == {}


def test_provision_requires_a_principal_key(suite, pc, mock_server):
    status, _body = pc.provision_plugin_agent_via_http(principal_key="")
    assert status == "failed"


def test_provision_rejects_the_principal_key_handed_back_as_the_agent_key(suite, pc, mock_server):
    """A server that returns the caller's own key would leave no isolation at
    all — and the principal resolvers skip the agent cache, so the launch
    would end up keyless. That is a bad response, not an identity."""
    mock_server.force_response(
        "POST",
        PROVISION_PATH[suite.name],
        200,
        {"apiKey": PRINCIPAL_KEY, "agentId": "agent-x", "created": True},
    )
    status, body = pc.provision_plugin_agent_via_http(principal_key=PRINCIPAL_KEY)
    assert (status, body) == ("failed", {})


def test_provision_requires_an_agent_id(suite, pc, mock_server):
    mock_server.force_response(
        "POST", PROVISION_PATH[suite.name], 200, {"apiKey": "agentkey-zzz", "created": True}
    )
    status, body = pc.provision_plugin_agent_via_http(principal_key=PRINCIPAL_KEY)
    assert (status, body) == ("failed", {})
    # The request IS made (the forced body is what gets rejected); nothing
    # from it may survive as a cached identity.
    assert pc.load_cached_agent_key(mock_server.url) == ""


def test_reprovision_does_not_rotate_the_old_key(suite, pc, mock_server):
    _, first = pc.provision_plugin_agent_via_http(principal_key=PRINCIPAL_KEY)
    status, _ = pc.provision_plugin_agent_via_http(principal_key=PRINCIPAL_KEY)
    assert status == "failed"
    assert mock_server.identity.valid_keys[first["api_key"]]["valid"] is True


# ── agent-key cache + key resolution ────────────────────────────────────────


def test_agent_key_cache_roundtrip_is_url_scoped(suite, pc):
    pc.save_cached_agent_key("http://one.test", "agent-key-1", "agent-1")
    assert pc.load_cached_agent_key("http://one.test") == "agent-key-1"
    assert pc.load_cached_agent_key("http://two.test") == ""
    pc.clear_cached_agent_key()
    assert pc.load_cached_agent_key("http://one.test") == ""


def test_api_key_resolution_prefers_the_plugin_identity(suite, pc, mock_server):
    pc.save_cached_agent_key(mock_server.url, "agent-key-1", "agent-1", principal_key=PRINCIPAL_KEY)
    key, source = pc._api_key_with_source(mock_server.url)
    assert (key, source) == ("agent-key-1", "plugin_agent_key")


def test_api_key_resolution_falls_back_to_env_without_identity(suite, pc, mock_server):
    key, source = pc._api_key_with_source(mock_server.url)
    assert (key, source) == (PRINCIPAL_KEY, "env_api_key")


# ── session-start bootstrap policy ──────────────────────────────────────────


@pytest.fixture
def bootstrap(suite, hook_module, mock_server, monkeypatch):
    """session-start.py loaded in-process, pointed at the mock server.

    Returns (module, run) where run(config) drives the full credential +
    registration bootstrap and returns its (user_id, api_key, name, ok) tuple.
    """
    module = hook_module(suite, "session-start.py")
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    monkeypatch.delenv("COGNEE_API_KEY", raising=False)

    def run(config: dict):
        config.setdefault("base_url", mock_server.url)
        config.setdefault("dataset", "agent_sessions")
        config.setdefault("user_email", "default_user@example.com")
        config.setdefault("user_password", "default_password")
        return asyncio.run(
            module._ensure_agent_credentials_and_register(
                config, "/tmp/project", "sess-1", "agent-sess-1", "sess-key"
            )
        )

    return module, run


def test_fresh_install_requires_explicit_identity_opt_in(suite, bootstrap, mock_server):
    module, run = bootstrap
    _user_id, api_key, _name, ok = run({"plugin_identity": True})
    assert ok
    assert api_key.startswith("agentkey-")
    mock_server.assert_called("POST", PROVISION_PATH[suite.name])
    mock_server.assert_called("POST", "/api/v1/agents/register", type=CONNECTION_TYPE[suite.name])


def test_existing_install_stays_on_the_principal_with_separated_memory(
    suite, bootstrap, mock_server, monkeypatch
):
    """A pre-existing env key marks a non-fresh install. With shared agent
    memory opted out there is nothing to keep the principal's datasets
    reachable from an agent, so no silent migration happens."""
    monkeypatch.setenv("COGNEE_API_KEY", PRINCIPAL_KEY)
    monkeypatch.setenv("COGNEE_SHARED_AGENT_MEMORY", "false")
    module, run = bootstrap
    _user_id, api_key, _name, ok = run({"api_key": PRINCIPAL_KEY})
    assert ok
    assert api_key == PRINCIPAL_KEY
    mock_server.assert_not_called("POST", PROVISION_PATH[suite.name])


def test_existing_install_migrates_under_shared_memory(suite, bootstrap, mock_server, monkeypatch):
    """Shared memory (the default) makes migration safe — the shared role keeps
    every principal-owned dataset reachable — so an existing install moves onto
    its plugin identity."""
    monkeypatch.setenv("COGNEE_API_KEY", PRINCIPAL_KEY)
    module, run = bootstrap
    _user_id, api_key, _name, ok = run({"api_key": PRINCIPAL_KEY})
    assert ok
    assert api_key.startswith("agentkey-")
    mock_server.assert_called("POST", PROVISION_PATH[suite.name])


def test_existing_install_reverts_when_shared_memory_is_unavailable(
    suite, bootstrap, mock_server, monkeypatch
):
    """The migration promise is 'nothing gets stranded'. When the server cannot
    wire shared memory (here: no permissions API), an existing install drops the
    freshly provisioned agent key and stays on the principal."""
    monkeypatch.setenv("COGNEE_API_KEY", PRINCIPAL_KEY)
    mock_server.identity.permissions_api = False
    module, run = bootstrap
    _user_id, api_key, _name, ok = run({"api_key": PRINCIPAL_KEY})
    assert ok
    assert api_key == PRINCIPAL_KEY
    mock_server.assert_called("POST", PROVISION_PATH[suite.name])
    # ...and the structural reason stops the next launch from provisioning again.
    mock_server.calls.clear()
    _user_id, api_key, _name, ok = run({"api_key": PRINCIPAL_KEY})
    assert ok and api_key == PRINCIPAL_KEY
    mock_server.assert_not_called("POST", PROVISION_PATH[suite.name])


def test_existing_install_provisions_on_opt_in(suite, bootstrap, mock_server, monkeypatch):
    monkeypatch.setenv("COGNEE_API_KEY", PRINCIPAL_KEY)
    module, run = bootstrap
    _user_id, api_key, _name, ok = run({"api_key": PRINCIPAL_KEY, "plugin_identity": "true"})
    assert ok
    assert api_key.startswith("agentkey-")
    mock_server.assert_called("POST", PROVISION_PATH[suite.name])


def test_cached_agent_key_is_reused_not_reprovisioned(suite, bootstrap, mock_server):
    module, run = bootstrap
    _u, first_key, _n, _ok = run({"plugin_identity": True})
    mock_server.calls.clear()
    _u, second_key, _n, ok = run({})
    assert ok
    assert second_key == first_key
    mock_server.assert_not_called("POST", PROVISION_PATH[suite.name])


def test_revoked_agent_key_stays_disconnected(suite, pc, bootstrap, mock_server):
    module, run = bootstrap
    _u, first_key, _n, _ok = run({"plugin_identity": True})
    mock_server.calls.clear()

    # Revoke out-of-band (dashboard disconnect / rotation on another machine):
    # /agents/register itself doesn't check keys in the fake, so force the 401.
    mock_server.identity.invalidate_key(first_key)
    mock_server.force_response("POST", "/api/v1/agents/register", 401, {"detail": "revoked"})

    # Under ``auto`` the rejected key is blocked and the launch falls back to
    # the principal — which the forced 401 also rejects, so the bootstrap
    # fails with the classified HTTP status; on the way it must never have
    # re-provisioned.
    with pytest.raises(urllib.error.HTTPError) as rejected:
        run({})
    assert rejected.value.code == 401
    calls = [c for c in mock_server.calls if c["path"] == PROVISION_PATH[suite.name]]
    assert len(calls) == 0
    assert pc._load_json_file(pc._AGENT_KEY_CACHE)["blocked"] is True
    # The data plane never uses the blocked key: the principal under ``auto``...
    key, source = pc._api_key_with_source(mock_server.url)
    assert key != first_key and source != "plugin_agent_key"
    # ...and an error under explicit identity.
    monkeypatch_env = pytest.MonkeyPatch()
    try:
        monkeypatch_env.setenv("COGNEE_PLUGIN_IDENTITY", "true")
        with pytest.raises(RuntimeError, match="rejected"):
            pc._api_key_with_source(mock_server.url)
        with pytest.raises(RuntimeError, match="disconnected"):
            run({"plugin_identity": True})
    finally:
        monkeypatch_env.undo()


def test_concurrent_startups_provision_only_once(suite, bootstrap, mock_server):
    from concurrent.futures import ThreadPoolExecutor

    module, _ = bootstrap

    def connect():
        return asyncio.run(
            module._ensure_plugin_identity(
                mock_server.url, {"plugin_identity": True}, PRINCIPAL_KEY
            )
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        keys = list(workers.map(lambda _: connect(), range(2)))
    assert keys[0] == keys[1]
    calls = [call for call in mock_server.calls if call["path"] == PROVISION_PATH[suite.name]]
    assert len(calls) == 1


def test_enabled_identity_does_not_fall_back_when_server_is_unsupported(
    suite, bootstrap, mock_server
):
    _, run = bootstrap
    mock_server.identity.plugin_provisioning = False
    with pytest.raises(RuntimeError, match="fallback is disabled"):
        run({"plugin_identity": True, "api_key": PRINCIPAL_KEY})
    mock_server.assert_not_called("POST", "/api/v1/agents/register")
