"""``plugin-keys.py`` — list and revoke the plugin agent's per-machine keys.

Every machine holds its own key for the plugin's agent (cognee#6113); the
server caps them. This script is how a user frees a slot: it lists the keys
most recently used first, marks the one this machine holds, and revokes the
one the user chose. It authenticates as the principal — the agent cannot
manage its own keys.
"""

from __future__ import annotations

import json

import pytest

PRINCIPAL_KEY = "test-api-key"


@pytest.fixture
def keys(suite, hook_module, isolated_modules, mock_server, monkeypatch):
    """The script's module, pointed at the mock server as the principal."""
    module = hook_module(suite, "plugin-keys.py")
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    monkeypatch.setenv("COGNEE_API_KEY", PRINCIPAL_KEY)
    return module


def _seed_keys(ident, plugin_key: str) -> list[dict]:
    """Three keys of the plugin's agent: used yesterday, used a month ago, never used."""
    rows = []
    for created, used in (
        ("2026-09-01T10:00:00Z", "2026-10-20T09:00:00Z"),
        ("2026-08-01T10:00:00Z", "2026-09-20T09:00:00Z"),
        ("2026-10-19T10:00:00Z", None),
    ):
        _, body = ident.plugins_provision(plugin_key, PRINCIPAL_KEY)
        ident.key_meta[body["apiKey"]] = {"created_at": created, "last_used_at": used}
        rows.append(
            {"api_key": body["apiKey"], "key_id": body["keyId"], "agent_id": body["agentId"]}
        )
    return rows


def _out(capsys) -> str:
    return capsys.readouterr().out


def test_list_sorts_by_last_use_and_marks_this_machine(suite, keys, mock_server, capsys):
    import _plugin_common as pc

    ident = mock_server.identity
    seeded = _seed_keys(ident, suite.name)
    mine = seeded[1]
    pc.save_cached_agent_key(
        mock_server.url,
        mine["api_key"],
        mine["agent_id"],
        principal_key=PRINCIPAL_KEY,
        key_id=mine["key_id"],
    )

    assert keys.main(["list", "--json"]) == 0
    listing = json.loads(_out(capsys))
    assert listing["ok"] is True
    ids = [row["id"] for row in listing["keys"]]
    # Used yesterday, used a month ago, never used — in that order.
    assert ids == [seeded[0]["key_id"], seeded[1]["key_id"], seeded[2]["key_id"]]
    assert [row["this_machine"] for row in listing["keys"]] == [False, True, False]
    assert listing["keys"][2]["last_used_at"] == ""
    # The request went out as the principal, never as the agent.
    entry = mock_server.assert_called("GET", f"/api/v1/integrations/plugins/{suite.name}/keys")
    assert entry["headers"].get("X-Api-Key") == PRINCIPAL_KEY

    assert keys.main(["list"]) == 0
    table = _out(capsys)
    assert "<- this machine" in table and "never" in table
    assert table.index(seeded[0]["key_id"]) < table.index(seeded[1]["key_id"])


def test_revoke_another_machines_key(suite, keys, mock_server, capsys):
    ident = mock_server.identity
    seeded = _seed_keys(ident, suite.name)
    assert keys.main(["revoke", seeded[2]["key_id"], "--json"]) == 0
    assert json.loads(_out(capsys))["revoked"] == seeded[2]["key_id"]
    assert ident.valid_keys[seeded[2]["api_key"]]["valid"] is False
    assert all(ident.valid_keys[row["api_key"]]["valid"] for row in seeded[:2])


def test_revoking_this_machines_key_needs_force_and_forgets_it(suite, keys, mock_server, capsys):
    import _plugin_common as pc

    ident = mock_server.identity
    (mine,) = _seed_keys(ident, suite.name)[:1]
    pc.save_cached_agent_key(
        mock_server.url,
        mine["api_key"],
        mine["agent_id"],
        principal_key=PRINCIPAL_KEY,
        key_id=mine["key_id"],
    )
    assert keys.main(["revoke", mine["key_id"], "--json"]) == 4
    assert json.loads(_out(capsys))["error"] == "this_machine"
    assert ident.valid_keys[mine["api_key"]]["valid"] is True
    mock_server.assert_not_called(
        "DELETE", f"/api/v1/integrations/plugins/{suite.name}/keys/{mine['key_id']}"
    )

    assert keys.main(["revoke", mine["key_id"], "--force", "--json"]) == 0
    assert json.loads(_out(capsys))["this_machine"] is True
    assert ident.valid_keys[mine["api_key"]]["valid"] is False
    # The next session start provisions afresh: nothing dead is left cached.
    assert pc.load_cached_agent_key(mock_server.url) == ""


def test_revoking_an_unknown_key_is_reported(suite, keys, mock_server, capsys):
    _seed_keys(mock_server.identity, suite.name)
    assert keys.main(["revoke", "key-does-not-exist", "--json"]) == 3
    assert json.loads(_out(capsys))["error"] == "not_found"


def test_server_without_key_routes_is_reported(suite, keys, mock_server, capsys):
    mock_server.identity.additive_keys = False
    assert keys.main(["list", "--json"]) == 5
    assert json.loads(_out(capsys))["error"] == "no_key_routes"


def test_all_lists_every_plugin_agent_of_the_user(suite, keys, mock_server, capsys):
    ident = mock_server.identity
    _seed_keys(ident, suite.name)
    other = "codex" if suite.name != "codex" else "claude-code"
    _, foreign = ident.plugins_provision(other, PRINCIPAL_KEY)
    assert keys.main(["list", "--all", "--json"]) == 0
    listing = json.loads(_out(capsys))
    assert {row["plugin"] for row in listing["keys"]} == {suite.name, other}
    assert foreign["keyId"] in {row["id"] for row in listing["keys"]}
    # Plugins the server knows but that were never provisioned simply list nothing.
    assert listing["unsupported"] == []
    assert keys.main(["list", "--all"]) == 0
    assert other in _out(capsys)


def test_authenticates_as_the_principal_even_when_the_env_key_is_the_agents(
    suite, keys, mock_server, monkeypatch, capsys
):
    import _plugin_common as pc

    ident = mock_server.identity
    (mine,) = _seed_keys(ident, suite.name)[:1]
    pc.save_cached_agent_key(
        mock_server.url,
        mine["api_key"],
        mine["agent_id"],
        principal_key=PRINCIPAL_KEY,
        key_id=mine["key_id"],
    )
    pc.save_cached_api_key(mock_server.url, PRINCIPAL_KEY)
    # Inside a session the hooks export the AGENT key; the listing must not use it.
    monkeypatch.setenv("COGNEE_API_KEY", mine["api_key"])
    assert keys.main(["list", "--json"]) == 0
    entry = mock_server.assert_called("GET", f"/api/v1/integrations/plugins/{suite.name}/keys")
    assert entry["headers"].get("X-Api-Key") == PRINCIPAL_KEY
    assert json.loads(_out(capsys))["keys"][0]["this_machine"] is True


def test_no_principal_key_is_an_error(suite, keys, monkeypatch, capsys):
    monkeypatch.delenv("COGNEE_API_KEY", raising=False)
    assert keys.main(["list", "--json"]) == 2
    assert json.loads(_out(capsys))["error"] == "no_principal_key"
