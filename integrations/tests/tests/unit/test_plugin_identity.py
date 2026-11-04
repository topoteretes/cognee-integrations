"""Plugin identity provisioning — the server's per-plugin agent registration.

Cognee now provisions a dedicated agent sub-user + labeled API key per plugin
(POST /api/v1/integrations/plugins/{plugin_key}/provision) and expects agent
connections to self-declare their type at /agents/register. The client policy
under test (see session-start.py `_ensure_plugin_identity`):

  - registrations self-declare the suite's connection type, not generic "api"
  - a cached agent key wins outright and is not re-provisioned
  - identity mode ``auto`` (default) provisions only in service of shared
    agent memory and falls back to the principal when that cannot be wired;
    ``true`` is explicit and never falls back to the owner; ``false`` runs as
    the principal
  - provisioning needs a server that only ever adds keys (cognee#6113, marker
    ``x-cognee-additive-plugin-keys`` on the provision operation): each machine
    holds its own key, a rejected key (401) is replaced in the same launch, and
    a full agent (409) is ``limit``
  - any other server — no plugin routes, or one that rotates every key on
    provision — is "unsupported" and never POSTed to: an error under ``true``,
    the principal under ``auto``
"""

from __future__ import annotations

import asyncio
import urllib.error

import pytest

#: Self-declared connection type per suite (KNOWN_AGENT_CONNECTION_TYPES).
CONNECTION_TYPE = {
    "claude-code": "claude_code",
    "codex": "codex",
    "antigravity": "antigravity",
    "cursor": "cursor",
}

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


# ── keys per machine: provisioning only ever adds a key ─────────────────────


def test_provisioning_on_another_machine_keeps_every_key(suite, pc, mock_server):
    other_machine = mock_server.identity.plugins_provision(suite.name, PRINCIPAL_KEY)[1]["apiKey"]
    status, body = pc.provision_plugin_agent_via_http(principal_key=PRINCIPAL_KEY)
    assert status == "provisioned"
    assert body["key_id"] and body["created"] is False
    ident = mock_server.identity
    assert ident.valid_keys[other_machine]["valid"] is True
    assert ident.valid_keys[body["api_key"]]["valid"] is True


def test_server_that_rotates_keys_is_never_provisioned_against(suite, pc, mock_server):
    """Released servers revoke every other key on provision: no marker, no POST."""
    ident = mock_server.identity
    ident.additive_keys = False
    other_machine = ident.plugins_provision(suite.name, PRINCIPAL_KEY)[1]["apiKey"]
    status, body = pc.provision_plugin_agent_via_http(principal_key=PRINCIPAL_KEY)
    assert (status, body) == ("unsupported", {})
    mock_server.assert_not_called("POST", PROVISION_PATH[suite.name])
    assert ident.valid_keys[other_machine]["valid"] is True


def test_unreadable_capability_is_not_permission_to_provision(suite, pc, mock_server):
    mock_server.force_response("GET", "/openapi.json", 500, {"detail": "boom"})
    status, body = pc.provision_plugin_agent_via_http(principal_key=PRINCIPAL_KEY)
    assert (status, body) == ("unsupported", {})
    mock_server.assert_not_called("POST", PROVISION_PATH[suite.name])
    # A transient failure is not remembered: the next launch asks again.
    mock_server.clear_forced()
    status, _ = pc.provision_plugin_agent_via_http(principal_key=PRINCIPAL_KEY)
    assert status == "provisioned"


def test_full_agent_on_an_additive_server_is_a_limit(suite, pc, mock_server):
    ident = mock_server.identity
    ident.max_agent_keys = 1
    other_machine = ident.plugins_provision(suite.name, PRINCIPAL_KEY)[1]["apiKey"]
    status, body = pc.provision_plugin_agent_via_http(principal_key=PRINCIPAL_KEY)
    assert (status, body) == ("limit", {})
    assert ident.valid_keys[other_machine]["valid"] is True


def test_limit_runs_as_the_principal_under_auto_and_raises_under_strict(
    suite, pc, bootstrap, mock_server
):
    ident = mock_server.identity
    ident.max_agent_keys = 1
    ident.plugins_provision(suite.name, PRINCIPAL_KEY)  # another machine holds the only slot
    _module, run = bootstrap
    _u, key, _n, ok = run({})
    assert ok and not key.startswith("agentkey-")
    assert pc.load_cached_agent_key(mock_server.url) == ""
    with pytest.raises(RuntimeError, match="maximum number of keys"):
        run({"plugin_identity": True})


def test_limit_fallback_warns_the_user_until_this_machine_gets_a_key(
    suite, pc, bootstrap, mock_server
):
    """Running on the personal key because the agent is full is not silent:
    SessionStart's systemMessage says so (top level, where the host shows it,
    and in hookSpecificOutput) until a slot frees up and this machine gets its
    own key."""
    ident = mock_server.identity
    ident.max_agent_keys = 1
    other_machine = ident.plugins_provision(suite.name, PRINCIPAL_KEY)[1]["apiKey"]
    module, run = bootstrap
    _u, key, _n, ok = run({})
    assert ok and not key.startswith("agentkey-")

    output = module._apply_key_limit_warning({"hookSpecificOutput": {"additionalContext": "x"}})
    message = output["systemMessage"]
    assert "personal API key" in message and pc.PLUGIN_LABEL in message
    # Points at the command that frees a slot, not at raw routes.
    assert pc.KEYS_COMMAND_HINT in message and "cognee-keys" in message
    assert output["hookSpecificOutput"]["systemMessage"] == message
    assert output["hookSpecificOutput"]["additionalContext"] == "x"

    # An old machine's key is revoked: the next launch provisions and is quiet.
    ident.invalidate_key(other_machine)
    _u, key, _n, ok = run({})
    assert ok and key.startswith("agentkey-")
    assert module._apply_key_limit_warning({}) == {}
    assert pc.key_limit_message() == ""


def test_doctor_names_the_key_limit_as_the_reason(suite, isolated_modules, mock_server):
    doctor = isolated_modules(suite, "doctor")
    import _plugin_common as common

    assert "key limit" not in doctor._resolve_memory_sharing()
    common.record_key_limit(mock_server.url)
    assert "key limit" in doctor._resolve_memory_sharing()


def test_rejected_key_is_replaced_in_the_same_launch_on_an_additive_server(
    suite, pc, bootstrap, mock_server, monkeypatch
):
    """Revoked out-of-band (dashboard, or pruned server-side): one more key
    revokes nothing, so the launch replaces it and runs as the agent — no
    degraded launch, no other machine touched."""
    ident = mock_server.identity
    module, run = bootstrap
    _u, first_key, _n, ok = run({})
    assert ok and first_key.startswith("agentkey-")
    other_machine = ident.plugins_provision(suite.name, PRINCIPAL_KEY)[1]["apiKey"]

    ident.register_checks_keys = True
    ident.invalidate_key(first_key)
    mock_server.calls.clear()

    _u, key, _n, ok = run({})
    assert ok and key.startswith("agentkey-") and key != first_key
    mock_server.assert_called("POST", PROVISION_PATH[suite.name])
    record = pc._load_json_file(pc._AGENT_KEY_CACHE)
    assert record["api_key"] == key and not record.get("blocked") and record["key_id"]
    assert ident.valid_keys[other_machine]["valid"] is True
    assert ident.valid_keys[key]["valid"] is True


def test_shared_memory_registration_binds_the_dataset_by_uuid_only(
    suite, pc, bootstrap, mock_server
):
    """The canonical dataset belongs to the parent, and the server resolves
    dataset names only among the caller's own datasets (cognee >= 1.6.3): the
    agent registering it by name is refused with 403. By UUID it is accepted."""
    mock_server.identity.register_checks_datasets = True
    _module, run = bootstrap
    _u, key, _n, ok = run({})
    assert ok and key.startswith("agentkey-")
    ident = mock_server.identity
    registered = [c for c in mock_server.calls if c["path"] == "/api/v1/agents/register"]
    assert registered and all(c["headers"].get("X-Api-Key") == key for c in registered)
    for call in registered:
        body = call.get("json") or {}
        assert "dataset_names" not in body
        # One id: the parent-owned canonical dataset.
        (dataset_id,) = body["dataset_ids"]
        assert ident.dataset_rows[dataset_id]["ownerId"] == ident.principal_id


def test_registration_refused_with_403_keeps_the_key_and_never_reprovisions(
    suite, pc, bootstrap, mock_server
):
    """403 is not a dead key: blocking it would make every launch mint another
    one until the agent is full."""
    ident = mock_server.identity
    _module, run = bootstrap
    _u, key, _n, ok = run({})
    assert ok and key.startswith("agentkey-")
    mock_server.calls.clear()
    mock_server.force_response("POST", "/api/v1/agents/register", 403, {"detail": "refused"})
    with pytest.raises(urllib.error.HTTPError):
        # The forced 403 also refuses the principal's registration.
        run({})
    mock_server.assert_not_called("POST", PROVISION_PATH[suite.name])
    record = pc._load_json_file(pc._AGENT_KEY_CACHE)
    assert record["api_key"] == key and not record.get("blocked")
    assert ident.valid_keys[key]["valid"] is True


# ── dataset readiness under shared memory ───────────────────────────────────


def _shared_memory_fixture(ident, suite_name):
    """A parent-owned dataset the agent can write ONLY through the shared role:
    tenant, role, agent membership, role grant, and the marker that names them."""
    _, tenant = ident.tenants_create(PRINCIPAL_KEY, "org")
    tenant_id = tenant["tenant_id"] if "tenant_id" in tenant else tenant["id"]
    _, role = ident.roles_create(PRINCIPAL_KEY, "cognee-agent")
    role_id = role["role_id"]
    _, provisioned = ident.plugins_provision(suite_name, PRINCIPAL_KEY)
    agent_id, agent_key = provisioned["agentId"], provisioned["apiKey"]
    assert ident.tenant_add_user(PRINCIPAL_KEY, agent_id, tenant_id)[0] == 200
    assert ident.role_add_user(PRINCIPAL_KEY, agent_id, role_id)[0] == 200
    assert ident.tenant_select(agent_key, tenant_id)[0] == 200
    # A UUID id, as the server mints them: ``ensure_dataset_ready_via_api``
    # takes the by-id path only for a UUID-shaped target (the fake's "ds-N"
    # ids would be treated as a NAME and created instead).
    import uuid

    dataset_id = str(uuid.uuid4())
    ident.dataset_rows[dataset_id] = {
        "id": dataset_id,
        "name": "shared",
        "ownerId": ident.principal_id,
        "tenant_id": tenant_id,
        "createdAt": "2026-01-01T00:00:00",
    }
    ident._grant(ident.principal_id, dataset_id, "read", "write", "delete", "share")
    assert ident.grant_datasets(PRINCIPAL_KEY, role_id, [dataset_id], "write")[0] == 200
    assert ident.grant_datasets(PRINCIPAL_KEY, role_id, [dataset_id], "read")[0] == 200
    marker = {
        "mode": "shared",
        "role_id": role_id,
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "parent_user_id": ident.principal_id,
    }
    return agent_key, dataset_id, marker


def test_readiness_accepts_a_dataset_writable_only_through_the_role(
    suite, pc, isolated_modules, mock_server, monkeypatch
):
    """``ensure_dataset_ready_via_api`` with a UUID target — the switch path.
    The agent holds no direct grant on the parent's dataset; write reaches it
    through the role, which only the union query sees."""
    import asyncio

    config = isolated_modules(suite, "config")
    agent_key, dataset_id, marker = _shared_memory_fixture(mock_server.identity, suite.name)
    pc._save_shared_memory_marker({"base_url": mock_server.url, **marker})
    monkeypatch.setenv("COGNEE_API_KEY", agent_key)

    asyncio.run(config.ensure_dataset_ready_via_api(mock_server.url, agent_key, dataset_id))
    direct = mock_server.identity.principal_datasets(marker["agent_id"], "write")
    assert dataset_id not in {row["id"] for row in direct}
    # By id, not by name: nothing was created.
    mock_server.assert_not_called("POST", "/api/v1/datasets")

    # Separated memory: the role is not consulted, and the direct query is empty.
    pc._save_shared_memory_marker({"base_url": mock_server.url, "mode": "separated"})
    with pytest.raises(RuntimeError, match="no verified write permission"):
        asyncio.run(config.ensure_dataset_ready_via_api(mock_server.url, agent_key, dataset_id))


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
