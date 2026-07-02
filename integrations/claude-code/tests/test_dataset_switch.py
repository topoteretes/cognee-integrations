"""Tests for mid-session dataset switching (dataset-switch.py + helpers).

Covers the acceptance criteria for a mid-session dataset switch:
  * the old ``(dataset, session_id)`` bridge is sealed (flushed + marked) and
    ``hook.log`` records ``old bridge sealed``;
  * the agent is re-registered against the new dataset with the SAME
    ``agent_session_name`` (conn_uuid) + ``session_id`` and ``hook.log`` records
    ``agent re-registered``;
  * the new dataset's high-water baseline is seeded so it receives only
    post-switch content (no duplicate graph writes);
  * switching to the already-active dataset is a no-op.

All I/O is redirected under a temp home and every network call is mocked, so
the suite is deterministic and needs no live Cognee server.

Run: python integrations/claude-code/tests/test_dataset_switch.py (or via pytest).
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import _plugin_common as pc  # noqa: E402
import config  # noqa: E402


def _load_dataset_switch():
    """Import the hyphenated dataset-switch.py module by path."""
    spec = importlib.util.spec_from_file_location(
        "dataset_switch", str(_SCRIPTS / "dataset-switch.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    """Redirect every plugin path under a temp home and clear COGNEE_* env."""
    home = tmp_path / "home"
    plugin = home / ".cognee-plugin" / "claude-code"
    shared = home / ".cognee-plugin"
    plugin.mkdir(parents=True)

    # _plugin_common module-level paths (computed from Path.home at import).
    monkeypatch.setattr(pc, "_PLUGIN_DIR", plugin)
    monkeypatch.setattr(pc, "_SHARED_PLUGIN_ROOT", shared)
    monkeypatch.setattr(pc, "_HOOK_LOG", plugin / "hook.log")
    monkeypatch.setattr(pc, "_SWITCH_STATE_FILE", plugin / "switch_state.json")
    monkeypatch.setattr(pc, "_BRIDGE_DIR", plugin / "bridge")
    monkeypatch.setattr(pc, "_PENDING_DIR", plugin / "pending")
    monkeypatch.setattr(pc, "_SESSIONS_MAP_DIR", plugin / "sessions")
    monkeypatch.setattr(pc, "_API_KEY_CACHE", shared / "api_key.json")

    # config module-level paths.
    monkeypatch.setattr(config, "_CONFIG_DIR", plugin)
    monkeypatch.setattr(config, "_STATE_DIR", plugin)
    monkeypatch.setattr(config, "_CONFIG_FILE", plugin / "config.json")
    monkeypatch.setattr(config, "_BRIDGE_STATE_FILE", plugin / "bridge_state.json")
    monkeypatch.setattr(config, "_HOOK_LOG", plugin / "hook.log")

    for key in list(os.environ.keys()):
        if key.startswith("COGNEE_") or key == "CLAUDE_CWD":
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("COGNEE_SESSION_KEY", "hostkey1")
    return home


def _read_hook_events():
    lines = pc._HOOK_LOG.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# --- state helpers -----------------------------------------------------------


def test_baseline_defaults_to_zero():
    assert pc.dataset_baseline("s1", "A") == (0, 0)


def test_baseline_roundtrip():
    pc.set_dataset_baseline("s1", "B", 5, 12)
    assert pc.dataset_baseline("s1", "B") == (5, 12)
    # An unrelated dataset in the same session is unaffected.
    assert pc.dataset_baseline("s1", "A") == (0, 0)


def test_sealed_marker_roundtrip():
    assert pc.is_dataset_sealed("s1", "A") is False
    pc.mark_dataset_sealed("s1", "A")
    assert pc.is_dataset_sealed("s1", "A") is True


def test_active_dataset_for_session():
    assert pc.active_dataset_for_session("s1") == ""
    pc.set_active_dataset_for_session("s1", "B")
    assert pc.active_dataset_for_session("s1") == "B"


def test_count_http_bridge_entries():
    pc.append_http_bridge_entry("A", "s1", question="q1", answer="a1")
    pc.append_http_bridge_entry("A", "s1", trace="t1")
    pc.append_http_bridge_entry("A", "s1", trace="t2")
    assert pc.count_http_bridge_entries("A", "s1") == (1, 2)
    # A different dataset bucket is counted independently.
    assert pc.count_http_bridge_entries("B", "s1") == (0, 0)


# --- seal --------------------------------------------------------------------


def test_seal_flushes_marks_and_logs(monkeypatch):
    pc.append_http_bridge_entry("A", "s1", question="q1", answer="a1")
    calls = {}

    def _fake_persist(dataset, session_id, timeout=600.0):
        calls["persist"] = (dataset, session_id)
        return True

    monkeypatch.setattr(pc, "persist_session_cache_to_graph_via_http", _fake_persist)

    result = pc.seal_bridge_state("A", "s1")

    assert calls["persist"] == ("A", "s1")
    assert result["sealed"] is True
    assert result["qa_count"] == 1
    assert result["flushed"] is True
    assert pc.is_dataset_sealed("s1", "A") is True

    events = _read_hook_events()
    sealed = [e for e in events if e["event"] == "dataset_switch_bridge_sealed"]
    assert sealed and sealed[0]["detail"]["message"] == "old bridge sealed"


# --- set_active_dataset ------------------------------------------------------


def test_set_active_dataset_writes_global_config():
    stores = config.set_active_dataset("B", cwd="")
    assert stores["config"] is True
    assert stores["picker"] is False
    saved = json.loads(config._CONFIG_FILE.read_text(encoding="utf-8"))
    assert saved["dataset"] == "B"
    assert os.environ["COGNEE_PLUGIN_DATASET"] == "B"


def test_set_active_dataset_preserves_other_config_keys():
    config._CONFIG_FILE.write_text(json.dumps({"agent_name": "keep-me"}), encoding="utf-8")
    config.set_active_dataset("B")
    saved = json.loads(config._CONFIG_FILE.read_text(encoding="utf-8"))
    assert saved["agent_name"] == "keep-me"
    assert saved["dataset"] == "B"


def test_set_active_dataset_updates_picker_when_present(tmp_path):
    project = tmp_path / "proj"
    cognee_dir = project / ".cognee"
    cognee_dir.mkdir(parents=True)
    picker = cognee_dir / "session-config.json"
    picker.write_text(json.dumps({"dataset": "A", "session_id": "keep"}), encoding="utf-8")

    stores = config.set_active_dataset("B", cwd=str(project))
    assert stores["picker"] is True
    updated = json.loads(picker.read_text(encoding="utf-8"))
    assert updated["dataset"] == "B"
    assert updated["session_id"] == "keep"  # unrelated keys preserved


def test_set_active_dataset_does_not_create_picker(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    stores = config.set_active_dataset("B", cwd=str(project))
    assert stores["picker"] is False
    assert not (project / ".cognee" / "session-config.json").exists()


# --- orchestration (http mode) ----------------------------------------------


def _wire_http_mode(monkeypatch, ds, register_result=(True, {"id": "conn-123"})):
    monkeypatch.setattr(ds, "http_api_ready", lambda: True)
    monkeypatch.setattr(pc, "persist_session_cache_to_graph_via_http", lambda *a, **k: True)
    captured = {}

    def _fake_register(*, agent_session_name, session_id="", dataset_names=None, timeout=15.0):
        captured["agent_session_name"] = agent_session_name
        captured["session_id"] = session_id
        captured["dataset_names"] = dataset_names
        return register_result

    monkeypatch.setattr(ds, "register_agent_via_http", _fake_register)
    return captured


def test_switch_http_seals_reregisters_and_seeds_baseline(monkeypatch):
    monkeypatch.setenv("COGNEE_SESSION_ID", "sess-1")
    config._CONFIG_FILE.write_text(json.dumps({"dataset": "A"}), encoding="utf-8")
    # Two pre-switch QA turns buffered under dataset A -> high-water = (2, 0).
    pc.append_http_bridge_entry("A", "sess-1", question="q1", answer="a1")
    pc.append_http_bridge_entry("A", "sess-1", question="q2", answer="a2")

    ds = _load_dataset_switch()
    captured = _wire_http_mode(monkeypatch, ds)

    import asyncio

    result = asyncio.run(ds.switch_dataset("B", cwd=""))

    assert result["status"] == "switched"
    assert result["old_dataset"] == "A"
    assert result["new_dataset"] == "B"

    # Old bridge sealed.
    assert pc.is_dataset_sealed("sess-1", "A") is True
    # Agent re-registered in place: same conn_uuid handle + session, new dataset.
    assert captured["session_id"] == "sess-1"
    assert captured["dataset_names"] == ["B"]
    assert captured["agent_session_name"].startswith("conn_")
    # New dataset seeded with the high-water baseline (2 pre-switch QA turns).
    assert pc.dataset_baseline("sess-1", "B") == (2, 0)
    # Active dataset persisted.
    assert config.get_dataset(config.load_config()) == "B"

    events = {e["event"] for e in _read_hook_events()}
    assert "dataset_switch_bridge_sealed" in events
    assert "dataset_switch_agent_reregistered" in events
    assert "dataset_switch_complete" in events


def test_switch_noop_when_same_dataset(monkeypatch):
    monkeypatch.setenv("COGNEE_SESSION_ID", "sess-1")
    config._CONFIG_FILE.write_text(json.dumps({"dataset": "A"}), encoding="utf-8")

    ds = _load_dataset_switch()
    captured = _wire_http_mode(monkeypatch, ds)

    import asyncio

    result = asyncio.run(ds.switch_dataset("A", cwd=""))

    assert result["status"] == "noop"
    assert result["reason"] == "already_active"
    assert "agent_session_name" not in captured  # never re-registered
    assert pc.is_dataset_sealed("sess-1", "A") is False


def test_switch_noop_when_no_new_dataset(monkeypatch):
    monkeypatch.setenv("COGNEE_SESSION_ID", "sess-1")
    config._CONFIG_FILE.write_text(json.dumps({"dataset": "A"}), encoding="utf-8")

    ds = _load_dataset_switch()
    _wire_http_mode(monkeypatch, ds)

    import asyncio

    result = asyncio.run(ds.switch_dataset("", cwd=""))
    assert result["status"] == "noop"
    assert result["reason"] == "no_new_dataset"


def test_resolve_new_dataset_precedence(monkeypatch):
    ds = _load_dataset_switch()
    # CLI arg wins over env and payload.
    monkeypatch.setenv("COGNEE_SWITCH_DATASET", "env-ds")
    assert ds._resolve_new_dataset({"new_dataset": "payload-ds"}, ["prog", "cli-ds"]) == "cli-ds"
    # env wins over payload.
    assert ds._resolve_new_dataset({"new_dataset": "payload-ds"}, ["prog"]) == "env-ds"
    monkeypatch.delenv("COGNEE_SWITCH_DATASET", raising=False)
    # payload fallback.
    assert ds._resolve_new_dataset({"dataset": "payload-ds"}, ["prog"]) == "payload-ds"
    assert ds._resolve_new_dataset({}, ["prog"]) == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
