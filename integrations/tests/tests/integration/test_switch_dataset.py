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
    monkeypatch.delenv("COGNEE_PLUGIN_DATASET", raising=False)
    monkeypatch.delenv("COGNEE_PLUGIN_READ_DATASET_IDS", raising=False)
    monkeypatch.delenv("COGNEE_PLUGIN_RECALL_MODE", raising=False)
    monkeypatch.setattr(pc, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(switch, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(switch, "_restart_idle_watcher", lambda *a, **k: None)
    monkeypatch.setattr(switch, "touch_activity", lambda: None)

    synced: list[tuple[str, str]] = []
    monkeypatch.setattr(
        switch, "_sync_current", lambda hk, sid, ds: synced.append((sid, ds)) or None
    )

    ident = mock_server.identity
    # ensure_dataset_ready_via_api only POSTs when it has a key to send. The
    # key must be the PRINCIPAL's: the fake scopes GET /datasets and dataset
    # creation to the calling user, like the server, and the datasets below
    # are seeded for the principal.
    monkeypatch.setenv("COGNEE_API_KEY", "test-api-key")
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


def test_switch_to_current_dataset_restricts_recall_without_a_new_session(env):
    """SDK-1393: picking the active dataset pins recall to it; picking it again
    is a no-op. Neither syncs nor registers a session."""
    result = env.switch._switch(env.host, env.rec(), "a", force=False)
    assert result["switched"] is False and result["reason"] == "recall_restricted"
    assert (result["recall"], result["session_id"]) == ("pinned", env.session_id)
    assert result["message"]
    assert env.rec()["recall_pinned"] is True
    again = env.switch._switch(env.host, env.rec(), "a", force=False)
    assert again["reason"] == "already_active" and again["message"]
    assert env.synced == [] and not _calls(env.server, REGISTER)


def test_auto_lifts_the_restriction_and_keeps_the_session(env):
    env.switch._switch(env.host, env.rec(), "a", force=False)
    result = env.switch._auto(env.host, env.rec())
    assert result["switched"] is False and result["reason"] == "recall_auto"
    assert (result["recall"], result["session_id"]) == ("auto", env.session_id)
    assert result["message"]
    assert env.rec()["recall_pinned"] is False
    again = env.switch._auto(env.host, env.rec())
    assert again["reason"] == "already_auto" and again["message"]
    assert env.synced == [] and not _calls(env.server, REGISTER)


def test_auto_under_a_read_set_is_saved_but_recall_stays_scoped(env, capsys, monkeypatch):
    """COGNEE_PLUGIN_READ_DATASET_IDS is an explicit scope ``auto`` cannot lift:
    the choice is saved, and every surface reports what is in effect."""
    monkeypatch.setenv("COGNEE_PLUGIN_READ_DATASET_IDS", "11111111-1111-4111-8111-111111111111")
    env.switch._switch(env.host, env.rec(), "a", force=False)
    result = env.switch._auto(env.host, env.rec())
    assert result["reason"] == "recall_auto" and result["recall"] == "pinned"
    assert result["recall_reason"] == "read_dataset_ids"
    assert "COGNEE_PLUGIN_READ_DATASET_IDS still restricts it" in result["message"]
    assert env.rec()["recall_pinned"] is False
    env.switch._print_switch(result, as_json=False)
    assert capsys.readouterr().out == result["message"] + "\n"
    listing = env.switch._list(env.host, env.rec())
    assert listing["recall"] == "pinned" and listing["reason"] == "read_dataset_ids"
    env.switch._print_listing(listing, as_json=False)
    out = capsys.readouterr().out
    assert "restricted to the datasets in COGNEE_PLUGIN_READ_DATASET_IDS" in out
    assert listing["message"] in out


# ── the pin action ─────────────────────────────────────────────────────────


def test_pin_pins_recall_to_the_active_dataset_without_a_new_session(env, capsys):
    import json

    before = env.rec()
    assert env.switch.main([" Pin ", "--json", "--session-key", env.host]) == env.switch.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["switched"] is False and out["reason"] == "recall_restricted"
    assert (out["recall"], out["dataset"]) == ("pinned", "a")
    rec = env.rec()
    assert rec["recall_pinned"] is True
    assert (rec["session_id"], rec["dataset"], rec["conn_uuid"]) == (
        before["session_id"],
        "a",
        before["conn_uuid"],
    )
    assert "switched_at" not in rec
    assert env.synced == [] and not _calls(env.server, REGISTER)
    assert not _calls(env.server, DATASETS)
    listing = env.switch._list(env.host, env.rec())
    assert listing["recall"] == "pinned" and listing["reason"] == "dataset_picked"
    assert "as picked in this session" in listing["message"]


def test_repicking_a_foreign_owned_active_dataset_by_name_only_pins(env, monkeypatch):
    """A dataset another principal owns is held in the record by its id, so a
    re-pick by name compared the name with the id and did a full switch. The
    resolved target is compared now: it only pins."""
    ident = env.server.identity
    row = ident.seed_dataset("team", owner_id="someone-else")
    ident._grant(ident.principal_id, row["id"], "write")
    monkeypatch.setattr(env.pc, "require_typed_dataset_id_support", lambda **kw: None)
    first = env.switch._switch(env.host, env.rec(), "team", force=False)
    assert first["switched"] is True and env.rec()["dataset"] == row["id"]
    env.switch._auto(env.host, env.rec())
    registered = len(_calls(env.server, REGISTER))
    session = env.rec()["session_id"]

    again = env.switch._switch(env.host, env.rec(), "team", force=False)
    assert again["switched"] is False and again["reason"] == "recall_restricted"
    assert again["recall"] == "pinned" and again["dataset"] == row["id"]
    assert env.rec()["recall_pinned"] is True and env.rec()["session_id"] == session
    assert len(_calls(env.server, REGISTER)) == registered
    assert len(env.synced) == 1  # only the first switch synced
    by_id = env.switch._switch(env.host, env.rec(), row["id"], force=False)
    assert by_id["switched"] is False and by_id["reason"] == "already_active"


# ── COGNEE_PLUGIN_RECALL_MODE ──────────────────────────────────────────────


@pytest.mark.parametrize("dataset", ["", "research"])
def test_recall_is_auto_by_default_whatever_the_configured_dataset(env, monkeypatch, dataset):
    """COGNEE_PLUGIN_DATASET names where saves go; it no longer pins recall."""
    monkeypatch.delenv("COGNEE_PLUGIN_RECALL_MODE", raising=False)
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", dataset)
    listing = env.switch._list(env.host, env.rec())
    assert listing["recall"] == "auto" and "reason" not in listing
    assert listing["message"].startswith("Recall is auto:")
    result = env.switch._auto(env.host, env.rec())
    assert result["reason"] == "already_auto" and "lifted" not in result
    # Saved explicitly anyway, so a later configuration cannot pin this launch.
    assert env.rec()["recall_pinned"] is False


def test_a_configured_pin_holds_until_auto_lifts_it(env, capsys, monkeypatch):
    """COGNEE_PLUGIN_RECALL_MODE=pinned pins recall; ``auto`` writes an explicit
    unpin that beats it for this launch, and the listing reports the mode in
    effect and why, in the same words as the text output."""
    import json

    monkeypatch.setenv("COGNEE_PLUGIN_RECALL_MODE", "pinned")
    assert env.switch.main(["--list", "--json", "--session-key", env.host]) == env.switch.EXIT_OK
    listing = json.loads(capsys.readouterr().out)
    assert listing["recall"] == "pinned" and listing["reason"] == "configured_pin"
    assert "COGNEE_PLUGIN_RECALL_MODE=pinned" in listing["message"]
    assert env.switch.main(["--list", "--session-key", env.host]) == env.switch.EXIT_OK
    assert listing["message"] in capsys.readouterr().out

    assert env.switch.main(["auto", "--json", "--session-key", env.host]) == env.switch.EXIT_OK
    lifted = json.loads(capsys.readouterr().out)
    assert lifted["reason"] == "recall_auto" and lifted["recall"] == "auto"
    assert lifted["lifted"] == "configured_pin"
    assert "COGNEE_PLUGIN_RECALL_MODE=pinned no longer applies" in lifted["message"]
    assert env.rec()["recall_pinned"] is False

    listing = env.switch._list(env.host, env.rec())
    assert listing["recall"] == "auto" and "reason" not in listing
    assert env.switch._auto(env.host, env.rec())["reason"] == "already_auto"
    assert env.synced == [] and not _calls(env.server, REGISTER)


def test_picking_the_active_dataset_under_a_configured_pin_records_the_pick(env, monkeypatch):
    monkeypatch.setenv("COGNEE_PLUGIN_RECALL_MODE", "pinned")
    result = env.switch._switch(env.host, env.rec(), "a", force=False)
    assert result["reason"] == "already_active" and result["recall"] == "pinned"
    assert env.rec()["recall_pinned"] is True
    assert env.switch._list(env.host, env.rec())["reason"] == "dataset_picked"
    # The pick outlives the configuration going back to auto.
    monkeypatch.setenv("COGNEE_PLUGIN_RECALL_MODE", "auto")
    assert env.switch._list(env.host, env.rec())["recall"] == "pinned"


# ── one message per result ─────────────────────────────────────────────────


def test_every_result_carries_the_message_the_text_output_prints(env, capsys):
    """--json and the text output say the same thing: listing, pin, auto and a
    switch each carry a ``message``, printed as is without --json."""
    import json

    runs = [["--list"], ["pin"], ["pin"], ["auto"], ["auto"], ["b"], ["b"]]
    for argv in runs:
        assert env.switch.main([*argv, "--json", "--session-key", env.host]) == env.switch.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert isinstance(out["message"], str) and out["message"].strip()
    # The text output of a listing prints the message under the current line.
    assert env.switch.main(["--list", "--session-key", env.host]) == env.switch.EXIT_OK
    text = capsys.readouterr().out.splitlines()
    listing = env.switch._list(env.host, env.rec())
    assert text[1] == listing["message"]


def test_auto_is_reserved_and_never_switches(env, capsys):
    import json

    env.switch._switch(env.host, env.rec(), "a", force=False)
    assert env.switch.main([" Auto ", "--json", "--session-key", env.host]) == env.switch.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["switched"] is False and out["reason"] == "recall_auto"
    assert env.rec()["dataset"] == "a" and not _calls(env.server, REGISTER)


def test_a_switch_pins_recall_to_the_new_dataset(env):
    result = env.switch._switch(env.host, env.rec(), "b", force=False)
    assert result["switched"] is True and result["recall"] == "pinned"
    assert env.rec()["recall_pinned"] is True


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
    assert "NOT synced" in result["message"]
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
