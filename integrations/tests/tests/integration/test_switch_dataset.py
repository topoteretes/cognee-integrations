"""switch-dataset.py against the mock server: listing, ordering, and the abort paths.

The switch is a sequence with a strict order — sync the retiring session, ensure
the target dataset, REGISTER the new session under a fresh handle, repoint the
record, then UNREGISTER the old handle — because a local agent-mode server shuts
down when its connection count reaches zero. These tests pin that order over
real HTTP and check that every failure leaves the launch record untouched.

The sync step spawns ``sync-session-to-graph.py`` as a subprocess; here it is
replaced with a recorder (its own strict behaviour is covered in
test_sync_strict.py), and the idle-watcher restart is a no-op.
"""

from __future__ import annotations

import pytest

HOST = "host-switch-1"
REGISTER, UNREGISTER, DATASETS = (
    "/api/v1/agents/register",
    "/api/v1/agents/unregister",
    "/api/v1/datasets",
)


@pytest.fixture
def env(suite, hook_module, isolated_modules, mock_server, monkeypatch):
    switch = hook_module(suite, "switch-dataset.py")
    pc = isolated_modules(suite, "_plugin_common")
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    monkeypatch.delenv("COGNEE_SESSION_KEY", raising=False)
    monkeypatch.delenv("COGNEE_SESSION_ID", raising=False)
    monkeypatch.setattr(pc, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(switch, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(switch, "_restart_idle_watcher", lambda *a, **k: None)
    monkeypatch.setattr(switch, "touch_activity", lambda: None)

    synced: list[tuple[str, str]] = []
    monkeypatch.setattr(
        switch, "_sync_current", lambda hk, sid, ds: synced.append((sid, ds)) or None
    )

    ident = mock_server.identity
    ident.seed_user("owner@example.com")
    # ensure_dataset_ready_via_api only POSTs when it has a key to send.
    monkeypatch.setenv("COGNEE_API_KEY", ident.seed_owner_key("owner@example.com"))
    # The launch as SessionStart left it: registered under conn_uuid on dataset "a".
    sid, conn = pc.ensure_launch_record(HOST, "/w", dataset="a")
    ident.agents_register({"agent_session_name": conn, "session_id": sid})
    ident.seed_dataset("a")
    ident.seed_dataset("b")
    ident.seed_dataset("shared-ro", owner_id="someone-else")

    class Env:
        pass

    e = Env()
    e.switch, e.pc, e.server, e.synced = switch, pc, mock_server, synced
    e.host, e.session_id, e.conn = HOST, sid, conn
    e.rec = lambda: pc._read_map_record(HOST)
    return e


def _calls(server, path):
    return [c for c in server.calls if c["path"] == path]


# ── listing ────────────────────────────────────────────────────────────────


def test_list_hides_foreign_datasets_and_marks_current(env):
    out = env.switch._list(env.host, env.rec())
    assert out["current"] == "a" and out["session_id"] == env.session_id
    names = {r["name"]: r for r in out["datasets"]}
    assert set(names) == {"a", "b"}
    assert names["a"]["current"] is True and names["b"]["current"] is False
    assert out["hidden_readonly"] == 1 and out["filtered"] is True


# ── the switch ─────────────────────────────────────────────────────────────


def test_switch_order_register_new_then_unregister_old(env):
    result = env.switch._switch(env.host, env.rec(), "b", force=False)
    assert result["switched"] is True and result["dataset"] == "b"

    # 1. the retiring session was synced into ITS dataset
    assert env.synced == [(env.session_id, "a")]

    # 2. register(new) happened before unregister(old), and named the new dataset
    order = [c for c in env.server.calls if c["path"] in (REGISTER, UNREGISTER)]
    assert [c["path"] for c in order] == [REGISTER, UNREGISTER]
    reg, unreg = order
    assert reg["json"]["dataset_names"] == ["b"]
    assert reg["json"]["session_id"] == result["session_id"]
    assert reg["json"]["agent_session_name"] == result["conn_uuid"] != env.conn
    assert unreg["json"]["agent_session_name"] == env.conn

    # 3. the record now points at the new triple, the old one is retired
    rec = env.rec()
    assert (rec["session_id"], rec["dataset"], rec["conn_uuid"]) == (
        result["session_id"],
        "b",
        result["conn_uuid"],
    )
    assert rec["switched_at"]
    pairs = env.pc.touched_pairs(env.host)
    assert [(p["session_id"], p["dataset"]) for p in pairs] == [
        (env.session_id, "a"),
        (result["session_id"], "b"),
    ]
    assert result["previous"] == {
        "dataset": "a",
        "session_id": env.session_id,
        "synced": True,
        "unregistered": True,
    }
    # the server's view: only the new handle is active
    assert set(env.server.identity.registered_agents) == {result["conn_uuid"]}


def test_switch_to_current_dataset_is_a_noop(env):
    result = env.switch._switch(env.host, env.rec(), "a", force=False)
    assert result == {
        "switched": False,
        "reason": "already_active",
        "dataset": "a",
        "session_id": env.session_id,
    }
    assert env.synced == [] and not _calls(env.server, REGISTER)


def test_switch_refuses_readonly_dataset_before_touching_anything(env):
    before = env.rec()
    with pytest.raises(env.switch.SwitchError) as exc:
        env.switch._switch(env.host, env.rec(), "shared-ro", force=False)
    assert exc.value.code == env.switch.EXIT_NOT_WRITABLE
    assert env.synced == [] and not _calls(env.server, REGISTER)
    assert env.rec() == before


def test_switch_creates_an_unlisted_dataset_for_the_principal(env):
    result = env.switch._switch(env.host, env.rec(), "brand-new", force=False)
    assert result["switched"] is True
    created = [c for c in env.server.calls if c["path"] == DATASETS and c["method"] == "POST"]
    assert created and created[0]["json"]["name"] == "brand-new"
    assert env.server.identity.datasets["brand-new"]["ownerId"] == env.server.identity.principal_id


def test_sync_failure_aborts_with_record_untouched(env, monkeypatch):
    def boom(hk, sid, ds):
        raise env.switch.SwitchError(env.switch.EXIT_SYNC_FAILED, "improve failed")

    monkeypatch.setattr(env.switch, "_sync_current", boom)
    before = env.rec()
    with pytest.raises(env.switch.SwitchError) as exc:
        env.switch._switch(env.host, env.rec(), "b", force=False)
    assert exc.value.code == env.switch.EXIT_SYNC_FAILED
    assert env.rec() == before
    assert not _calls(env.server, REGISTER) and not _calls(env.server, UNREGISTER)


def test_force_switches_past_sync_failure_and_reports_it(env, monkeypatch):
    def boom(hk, sid, ds):
        raise env.switch.SwitchError(env.switch.EXIT_SYNC_FAILED, "improve failed")

    monkeypatch.setattr(env.switch, "_sync_current", boom)
    result = env.switch._switch(env.host, env.rec(), "b", force=True)
    assert result["switched"] is True
    assert result["previous"]["synced"] is False
    assert "improve failed" in result["previous"]["sync_error"]
    # the retired pair is still in `touched`, so the final sync retries it
    assert (env.session_id, "a") in {
        (p["session_id"], p["dataset"]) for p in env.pc.touched_pairs(env.host)
    }


def test_register_failure_changes_nothing(env):
    env.server.force_response("POST", REGISTER, 500, {"detail": "boom"})
    before = env.rec()
    with pytest.raises(env.switch.SwitchError) as exc:
        env.switch._switch(env.host, env.rec(), "b", force=False)
    assert exc.value.code == env.switch.EXIT_REGISTER_FAILED
    assert env.synced == [(env.session_id, "a")]  # sync ran first, by design
    assert env.rec() == before
    assert not _calls(env.server, UNREGISTER)
    assert set(env.server.identity.registered_agents) == {env.conn}


# ── CLI surface ────────────────────────────────────────────────────────────


def test_main_list_json_and_missing_record(env, capsys, monkeypatch):
    import json

    assert env.switch.main(["--list", "--json", "--session-key", env.host]) == env.switch.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["current"] == "a" and {r["name"] for r in out["datasets"]} == {"a", "b"}

    # No flag, no hook env, and discovery finds nothing: a clear "no record" exit.
    monkeypatch.delenv("COGNEE_SESSION_KEY", raising=False)
    monkeypatch.setattr(
        env.switch, "resolve_host_key_outside_hook", lambda cwd="": ("", "not_found")
    )
    assert env.switch.main(["--list", "--json"]) == env.switch.EXIT_NO_RECORD
    assert json.loads(capsys.readouterr().out)["code"] == env.switch.EXIT_NO_RECORD


# ── final sync covers retired sessions ─────────────────────────────────────


def test_final_sync_targets_include_touched_pairs(suite, hook_module, env, monkeypatch):
    env.switch._switch(env.host, env.rec(), "b", force=False)
    sync = hook_module(suite, "sync-session-to-graph.py")
    rec = env.rec()
    targets = sync._sync_targets(rec["session_id"], "b", env.host, include_touched=True)
    assert targets == [(env.session_id, "a"), (rec["session_id"], "b")]
    # a mid-session manual sync stays scoped to the live pair
    assert sync._sync_targets(rec["session_id"], "b", env.host, include_touched=False) == [
        (rec["session_id"], "b")
    ]
    handles = sync._unregister_handles(env.host, rec["conn_uuid"])
    assert handles == [env.conn, rec["conn_uuid"]]
