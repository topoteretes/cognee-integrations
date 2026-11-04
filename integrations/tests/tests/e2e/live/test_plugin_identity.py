"""Plugin identity provisioning against a real cognee server (SDK-1002).

The unit tier proves the client against a fake that models the server's plugin
routes. What only a real server can prove is the contract itself: that the
provision route only adds a key per machine (cognee#6113), that a revoked key
is replaced in the same session start, that a typed session entry addressed by
name is accepted, and that the session still lands in the parent-owned shared
dataset at sync. Every scenario here boots its own server in a temp HOME.

The plugin provisions only against a server that advertises additive plugin
keys; on any other (the pinned local version until it moves past cognee#6113)
every scenario skips.

All scenarios are ``local_only``: they provision and revoke the plugin's keys,
which against a real tenant would touch the developer's own plugin identity.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from utils.hooklog import hook_events
from utils.live import _api_request, server_health
from utils.suites import Suite, state_dir

pytestmark = [pytest.mark.live, pytest.mark.local_only]

BOOT_DEADLINE = 900.0


def _start(session, base_url: str):
    run = session.start()
    assert run.ok, f"SessionStart failed (rc={run.returncode}): {run.stderr[:800]}"
    deadline = time.monotonic() + BOOT_DEADLINE
    while time.monotonic() < deadline:
        if server_health(base_url) == 200:
            return run
        time.sleep(2.0)
    raise AssertionError(f"no healthy server on {base_url} within {BOOT_DEADLINE}s")


def _principal_key(home: Path) -> str:
    cache = home / ".cognee-plugin" / "api_key.json"
    assert cache.exists(), "the plugin never minted a principal key"
    return str(json.loads(cache.read_text(encoding="utf-8")).get("api_key") or "")


def _agent_record(suite: Suite, home: Path) -> dict:
    path = state_dir(suite, home) / "agent_key.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _marker(suite: Suite, home: Path) -> dict:
    path = state_dir(suite, home) / "shared_memory.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _events(suite: Suite, home: Path, name: str) -> list[dict]:
    return [detail for event, detail in hook_events(suite, home) if event == name]


def _plugin_status(base_url: str, api_key: str, plugin_key: str) -> dict:
    status, body = _api_request(base_url, "/api/v1/integrations/status", api_key)
    assert status == 200, f"status route answered {status}: {body[:300]}"
    rows = [row for row in json.loads(body).get("plugins", []) if row.get("key") == plugin_key]
    return rows[0] if rows else {}


def _require_known_plugin(home: Path, base_url: str, plugin_key: str) -> str:
    """The principal key — or a skip when this server cannot provision the plugin.

    Two reasons: the server predates additive plugin keys (cognee#6113), so the
    plugin never provisions against it; or it has no registry entry for the
    plugin (the status route lists every KNOWN plugin, provisioned or not; an
    absent row means provisioning 404s: SDK-582 for antigravity).
    """
    principal_key = _principal_key(home)
    status, body = _api_request(base_url, "/openapi.json", principal_key)
    operation = (
        json.loads(body).get("paths", {}).get(_PROVISION, {}).get("post", {})
        if status == 200
        else {}
    )
    if operation.get("x-cognee-additive-plugin-keys") is not True:
        pytest.skip("this cognee predates additive plugin keys (cognee#6113)")
    if not _plugin_status(base_url, principal_key, plugin_key):
        pytest.skip(f"this cognee does not register the {plugin_key!r} plugin (SDK-582)")
    return principal_key


_PROVISION = "/api/v1/integrations/plugins/{plugin_key}/provision"


def _datasets(base_url: str, api_key: str) -> list[dict]:
    status, body = _api_request(base_url, "/api/v1/datasets", api_key)
    assert status == 200, f"/datasets answered {status} for this key"
    return json.loads(body)


def _canonical_dataset(base_url: str, principal_key: str, name: str) -> dict:
    """The parent-owned dataset of this name — the one every agent writes to."""
    status, me = _api_request(base_url, "/api/v1/users/me", principal_key)
    assert status == 200
    principal_id = str(json.loads(me).get("id"))
    rows = [
        d
        for d in _datasets(base_url, principal_key)
        if d.get("name") == name and str(d.get("owner_id") or d.get("ownerId")) == principal_id
    ]
    assert rows, f"no parent-owned {name!r}: the canonical dataset was not created"
    return rows[0]


def test_fresh_install_provisions_and_accepts_typed_writes(
    live_session_factory, live_suite, live_home, live_base_url, live_dataset, nonce
):
    """The headline: a fresh install on a released server gets its identity and
    the shared role, and the typed session writes the server used to refuse by
    UUID are accepted by name. No LLM call is involved."""
    session = live_session_factory("fresh")
    _start(session, live_base_url)
    principal_key = _require_known_plugin(live_home, live_base_url, live_suite.name)

    provisioned = _events(live_suite, live_home, "plugin_agent_provisioned")
    skipped = _events(live_suite, live_home, "plugin_provision_skipped")
    assert provisioned and provisioned[-1].get("created") is True, (
        f"no provisioning on a fresh install: {skipped}"
    )
    record = _agent_record(live_suite, live_home)
    assert record.get("api_key") and record.get("agent_id") and not record.get("blocked")
    marker = _marker(live_suite, live_home)
    assert marker.get("mode") == "shared", f"shared memory did not wire: {marker}"

    # The server agrees: this plugin's identity row is connected.
    row = _plugin_status(live_base_url, principal_key, live_suite.name)
    assert row.get("connected") is True and row.get("source") == "identity", row
    assert str(row.get("agentId")) == str(record["agent_id"])

    # The canonical dataset is the parent's, and the agent reaches it through
    # the role: it lists for the agent key although the agent owns nothing.
    canonical = _canonical_dataset(live_base_url, principal_key, live_dataset)
    agent_view = {str(d.get("id")) for d in _datasets(live_base_url, record["api_key"])}
    assert str(canonical["id"]) in agent_view, "the agent cannot read the parent's dataset"

    # Typed entries addressed by name are accepted (the UUID write used to 400).
    assert session.prompt(f"Remember {nonce}: identity live test.", turn_id="t1").ok
    assert session.answer(f"Noted {nonce}.", turn_id="t1").ok
    assert _events(live_suite, live_home, "stop_stored"), "the answer was not stored"
    for failure in ("prompt_run_exception", "run_exception", "store_buffered_warming"):
        assert not _events(live_suite, live_home, failure), failure
    session.end()


def test_synced_session_lands_in_the_parent_owned_dataset(
    live_session_factory, live_suite, live_home, live_base_url, live_dataset, nonce
):
    """The sync names the canonical dataset by UUID, so what the agent captured
    ends up as the parent's data. Needs a real LLM key: the sync cognifies."""
    session = live_session_factory("synced")
    _start(session, live_base_url)
    principal_key = _require_known_plugin(live_home, live_base_url, live_suite.name)
    assert _marker(live_suite, live_home).get("mode") == "shared"
    assert session.prompt(f"For {nonce} we chose Raft.", turn_id="t1").ok
    assert session.answer(f"Recorded: {nonce} uses Raft.", turn_id="t1").ok
    session.end()

    bridged = session.wait_for_sync(deadline=600.0)
    assert bridged is not None and bridged.get("wrote") is True, bridged
    canonical = _canonical_dataset(live_base_url, principal_key, live_dataset)
    status, body = _api_request(
        live_base_url, f"/api/v1/datasets/{canonical['id']}/data", principal_key
    )
    assert status == 200, (status, body[:300])
    assert json.loads(body), "the parent-owned dataset holds no data after the sync"


def test_restart_reuses_the_cached_identity_without_provisioning(
    live_session_factory, live_suite, live_home, live_base_url
):
    first = live_session_factory("first")
    _start(first, live_base_url)
    _require_known_plugin(live_home, live_base_url, live_suite.name)
    before = _agent_record(live_suite, live_home)
    assert before.get("api_key")
    first.end()

    second = live_session_factory("second")
    _start(second, live_base_url)
    assert _agent_record(live_suite, live_home).get("api_key") == before["api_key"]
    assert len(_events(live_suite, live_home, "plugin_agent_provisioned")) == 1
    second.end()


def test_install_that_ran_on_the_principal_provisions_on_upgrade(
    live_session_factory, live_suite, live_home, live_base_url, nonce
):
    """The upgrade path: an install that used the principal key has sessions
    under the plugin's name, so the status route reports it ``connected`` from
    legacy sessions. That must not read as an existing identity."""
    legacy = live_session_factory("legacy")
    legacy.env = {**legacy.env, "COGNEE_PLUGIN_IDENTITY": "false"}
    _start(legacy, live_base_url)
    assert legacy.prompt(f"{nonce} on the principal.", turn_id="t1").ok
    assert legacy.answer(f"Stored {nonce}.", turn_id="t1").ok
    legacy.end()
    assert not _agent_record(live_suite, live_home)

    principal_key = _require_known_plugin(live_home, live_base_url, live_suite.name)
    row = _plugin_status(live_base_url, principal_key, live_suite.name)
    assert row.get("connected") is True and row.get("source") != "identity", (
        f"expected a legacy-connected row before the upgrade: {row}"
    )

    upgraded = live_session_factory("upgraded")
    _start(upgraded, live_base_url)
    provisioned = _events(live_suite, live_home, "plugin_agent_provisioned")
    skipped = _events(live_suite, live_home, "plugin_provision_skipped")
    assert provisioned and provisioned[-1].get("created") is True, (
        f"legacy sessions blocked provisioning: {skipped}"
    )
    assert _plugin_status(live_base_url, principal_key, live_suite.name).get("source") == "identity"
    upgraded.end()


def test_revoked_key_is_replaced_in_the_same_session_start(
    live_session_factory, live_suite, live_home, live_base_url
):
    """A dashboard revoke (or a server-side prune): the next session start sees
    the 401, mints one more key and runs as the agent — no degraded launch."""
    first = live_session_factory("first")
    _start(first, live_base_url)
    principal_key = _require_known_plugin(live_home, live_base_url, live_suite.name)
    old = _agent_record(live_suite, live_home)
    first.end()

    status, body = _api_request(
        live_base_url,
        f"/api/v1/integrations/plugins/{live_suite.name}/keys/{old['key_id']}",
        principal_key,
        method="DELETE",
    )
    assert status == 200 and json.loads(body).get("revoked") is True, (status, body)

    replaced = live_session_factory("replaced")
    _start(replaced, live_base_url)
    record = _agent_record(live_suite, live_home)
    assert record.get("api_key") and record["api_key"] != old["api_key"]
    assert not record.get("blocked")
    assert _events(live_suite, live_home, "plugin_identity_reprovisioned")
    assert not _events(live_suite, live_home, "plugin_identity_rejected_fallback")
    assert _marker(live_suite, live_home).get("mode") == "shared"
    replaced.end()


def test_a_second_machine_gets_its_own_key_and_logs_nobody_out(
    live_session_factory, live_suite, live_home, live_base_url
):
    """Another machine provisions for the same agent: both keys stay valid."""
    first = live_session_factory("first")
    _start(first, live_base_url)
    principal_key = _require_known_plugin(live_home, live_base_url, live_suite.name)
    mine = _agent_record(live_suite, live_home)
    first.end()

    status, body = _api_request(
        live_base_url,
        f"/api/v1/integrations/plugins/{live_suite.name}/provision",
        principal_key,
        method="POST",
    )
    assert status in (200, 201), (status, body)
    other = json.loads(body)
    other_key = other.get("api_key") or other.get("apiKey")
    assert other_key and other_key != mine["api_key"]
    assert str(other.get("agent_id") or other.get("agentId")) == str(mine["agent_id"])

    for key in (mine["api_key"], other_key):
        assert _api_request(live_base_url, "/api/v1/users/me", key)[0] == 200

    again = live_session_factory("again")
    _start(again, live_base_url)
    assert _agent_record(live_suite, live_home)["api_key"] == mine["api_key"]
    assert len(_events(live_suite, live_home, "plugin_agent_provisioned")) == 1
    again.end()
