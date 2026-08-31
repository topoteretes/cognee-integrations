"""Dataset switching: the launch record as the single source of the active dataset.

A launch's dataset used to be process-local (COGNEE_PLUGIN_DATASET or the
default, read by every hook independently). ``switch-dataset.py`` moves a launch
to another dataset mid-session, so the dataset now lives in the host-keyed launch
record and every reader — hooks via ``config.get_dataset``, the shell wrappers,
the watchers, the status line — follows the record.

Covers, for both suites:
  * record seeding at SessionStart and the env/default fallback outside a launch
  * a switched record beating an exported COGNEE_SESSION_ID
  * ``touched`` history (legacy string list and the new triple list)
  * switch session-id minting (``{agent}_{host}__N``, never colliding)
  * ``switch_launch_record`` atomically replacing the live triple
  * the writable-dataset filter (owner match, camelCase wire key, unknown owner)
  * launch-record discovery from a process with no hook payload
  * the status line reading the record's dataset
"""

from __future__ import annotations

import json

import pytest

HOST = "host-abc-123"


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    monkeypatch.delenv("COGNEE_SESSION_KEY", raising=False)
    monkeypatch.delenv("COGNEE_SESSION_ID", raising=False)
    monkeypatch.delenv("COGNEE_PLUGIN_DATASET", raising=False)
    return common


# ── record seeding + resolution ─────────────────────────────────────────────


def test_launch_record_seeds_dataset_cwd_and_pid(pc):
    sid, conn = pc.ensure_launch_record(HOST, "/work/proj", dataset="seeded", host_pid=4242)
    rec = pc._read_map_record(HOST)
    assert rec["session_id"] == sid and rec["conn_uuid"] == conn
    assert rec["dataset"] == "seeded"
    assert rec["cwd"] == "/work/proj"
    assert rec["host_pid"] == 4242
    assert "switched_at" not in rec


def test_resume_keeps_recorded_dataset_over_new_seed(pc):
    """A resumed launch (record exists) keeps its dataset — incl. a switched one —
    even when the shell now exports a different COGNEE_PLUGIN_DATASET."""
    pc.ensure_launch_record(HOST, "/w", dataset="first")
    pc.ensure_launch_record(HOST, "/w", dataset="from-new-shell")
    assert pc._read_map_record(HOST)["dataset"] == "first"


def test_backfill_adds_missing_metadata_only(pc):
    pc.ensure_launch_record(HOST, "/w")  # legacy-shaped: no dataset/cwd/pid
    assert "dataset" not in pc._read_map_record(HOST)
    pc.ensure_launch_record(HOST, "/w", dataset="later", host_pid=7)
    rec = pc._read_map_record(HOST)
    assert rec["dataset"] == "later" and rec["host_pid"] == 7


def test_resolve_active_dataset_precedence(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "from-env")
    # outside a launch: env
    assert pc.resolve_active_dataset("") == "from-env"
    # inside a launch without a recorded dataset: env
    pc.ensure_launch_record(HOST, "/w")
    assert pc.resolve_active_dataset(HOST) == "from-env"
    # recorded: record wins
    pc.ensure_launch_record(HOST, "/w", dataset="recorded")
    assert pc.resolve_active_dataset(HOST) == "recorded"
    # no env, no record: default
    monkeypatch.delenv("COGNEE_PLUGIN_DATASET")
    assert pc.resolve_active_dataset("nope") == "agent_sessions"


def test_config_get_dataset_follows_record(suite, pc, isolated_modules, monkeypatch):
    config = isolated_modules(suite, "config")
    cfg = {"dataset": "from-config"}
    assert config.get_dataset(cfg) == "from-config"  # no launch → config value
    pc.ensure_launch_record(HOST, "/w", dataset="from-config")
    pc.switch_launch_record(HOST, session_id="s2", dataset="switched", conn_uuid="c2")
    monkeypatch.setenv("COGNEE_SESSION_KEY", HOST)
    assert config.get_dataset(cfg) == "switched"


def test_load_resolved_carries_dataset(pc, monkeypatch):
    pc.ensure_launch_record(HOST, "/w", dataset="ds-a")
    monkeypatch.setattr(pc, "_json_http_request", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert pc.load_resolved(HOST)["dataset"] == "ds-a"


# ── session id precedence after a switch ───────────────────────────────────


def test_switched_record_beats_exported_session_id(pc, monkeypatch):
    pc.ensure_launch_record(HOST, "/w", dataset="a")
    monkeypatch.setenv("COGNEE_SESSION_ID", "pinned_by_shell")
    assert pc.resolve_cognee_session_id(HOST) == "pinned_by_shell"  # unchanged before
    pc.switch_launch_record(HOST, session_id="moved", dataset="b", conn_uuid="c")
    assert pc.resolve_cognee_session_id(HOST) == "moved"


# ── touched history + id minting ───────────────────────────────────────────


def test_touched_pairs_legacy_string_list(pc):
    pc._write_map_record(
        HOST,
        {"session_id": "cur", "conn_uuid": "c1", "dataset": "d", "touched": ["old", "cur"]},
    )
    pairs = pc.touched_pairs(HOST)
    assert [p["session_id"] for p in pairs] == ["old", "cur"]
    assert all(p["dataset"] == "d" for p in pairs)
    assert pairs[-1]["conn_uuid"] == "c1"


def test_switch_record_replaces_live_triple_and_retires_old(pc):
    sid, conn = pc.ensure_launch_record(HOST, "/w", dataset="a")
    rec = pc.switch_launch_record(HOST, session_id="s2", dataset="b", conn_uuid="c2")
    assert (rec["session_id"], rec["dataset"], rec["conn_uuid"]) == ("s2", "b", "c2")
    assert rec["switched_at"]
    pairs = pc.touched_pairs(HOST)
    assert [(p["session_id"], p["dataset"], p["conn_uuid"]) for p in pairs] == [
        (sid, "a", conn),
        ("s2", "b", "c2"),
    ]
    assert rec["touched"][0]["to"] and rec["touched"][1]["from"]


def test_mint_switch_session_id_never_collides(suite, pc):
    pc.ensure_launch_record(HOST, "/w", dataset="a")
    base = f"{suite.session_prefix}_{HOST}"
    first = pc.mint_switch_session_id(HOST)
    assert first == f"{base}__2"
    pc.switch_launch_record(HOST, session_id=first, dataset="b", conn_uuid="c2")
    second = pc.mint_switch_session_id(HOST)
    assert second == f"{base}__3"
    assert second not in {p["session_id"] for p in pc.touched_pairs(HOST)}


# ── writable-dataset filter ────────────────────────────────────────────────


def _serve(pc, monkeypatch, rows):
    monkeypatch.setattr(pc, "_json_http_request", lambda *a, **k: rows)


def test_list_writable_filters_by_owner_camelcase(pc, monkeypatch):
    _serve(
        pc,
        monkeypatch,
        [
            {"name": "mine", "id": "1", "ownerId": "me"},
            {"name": "theirs", "id": "2", "ownerId": "someone"},
            {"name": "also-mine", "id": "3", "owner_id": "me"},
        ],
    )
    out = pc.list_writable_datasets("me")
    assert [r["name"] for r in out["datasets"]] == ["also-mine", "mine"]
    assert out["readonly"] == ["theirs"]
    assert out["hidden_readonly"] == 1 and out["filtered"] is True


def test_list_writable_unknown_owner_is_kept_not_verified(pc, monkeypatch):
    """A pre-1.6 server has no owner in the DTO: show everything, say so."""
    _serve(pc, monkeypatch, [{"name": "x", "id": "1"}, {"name": "y", "id": "2"}])
    out = pc.list_writable_datasets("me")
    assert [r["name"] for r in out["datasets"]] == ["x", "y"]
    assert all(r["writable"] is None for r in out["datasets"])
    assert out["filtered"] is False and out["hidden_readonly"] == 0


def test_list_writable_without_user_id_is_unfiltered(pc, monkeypatch):
    _serve(pc, monkeypatch, [{"name": "x", "id": "1", "ownerId": "someone"}])
    out = pc.list_writable_datasets("")
    assert [r["name"] for r in out["datasets"]] == ["x"] and out["filtered"] is False


# ── launch discovery without a hook payload ────────────────────────────────


def test_host_key_from_env_session_key(pc, monkeypatch):
    pc.ensure_launch_record(HOST, "/w", dataset="a")
    monkeypatch.setenv("COGNEE_SESSION_KEY", HOST)
    assert pc.resolve_host_key_outside_hook() == (HOST, "env_session_key")


def test_host_key_from_claude_session_export(pc, monkeypatch):
    pc.ensure_launch_record(HOST, "/w", dataset="a")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", HOST)
    assert pc.resolve_host_key_outside_hook() == (HOST, "CLAUDE_CODE_SESSION_ID")


def test_host_key_from_unique_cwd(pc, monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(pc, "_candidate_host_pids", lambda: set())
    pc.ensure_launch_record(HOST, str(tmp_path), dataset="a")
    assert pc.resolve_host_key_outside_hook(str(tmp_path)) == (HOST, "cwd")
    pc.ensure_launch_record("other-host", str(tmp_path), dataset="a")
    assert pc.resolve_host_key_outside_hook(str(tmp_path)) == ("", "ambiguous_cwd")


def test_host_key_from_host_pid(pc, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    pc.ensure_launch_record(HOST, "/w", dataset="a", host_pid=999_999)
    monkeypatch.setattr(pc._proc, "pid_alive", lambda pid: True)
    import _proc

    monkeypatch.setattr(_proc, "pid_alive", lambda pid: True)
    monkeypatch.setattr(pc, "_candidate_host_pids", lambda: {999_999})
    assert pc.resolve_host_key_outside_hook("/elsewhere") == (HOST, "host_pid")


# ── status line ────────────────────────────────────────────────────────────


def test_statusline_reads_record_dataset(suite, statusline, temp_home, monkeypatch):
    sessions = temp_home / ".cognee-plugin" / suite.state_subdir / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{HOST}.json").write_text(
        json.dumps({"session_id": "s", "dataset": "from-record", "switched_at": "2026-01-01"})
    )
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "from-env")
    assert statusline._active_dataset(HOST) == "from-record"
    assert "switched" in statusline._switched_marker(HOST)
    # another launch, or no host id: env still rules and no marker
    assert statusline._active_dataset("unknown-host") == "from-env"
    assert statusline._switched_marker("unknown-host") == ""
    assert statusline._active_dataset("../evil") == "from-env"  # path-unsafe id ignored
