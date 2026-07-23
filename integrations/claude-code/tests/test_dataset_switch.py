"""Tests for mid-session dataset switching (dataset-switch.py + helpers).

Covers the acceptance criteria for a mid-session dataset switch:
  * subsequent hooks resolve the NEW dataset after a switch (``get_dataset``
    follows the launch-scoped override), so new memory is written there;
  * the old ``(dataset, session_id)`` bridge is sealed (flushed + ``hook.log``
    records ``old bridge sealed``);
  * the agent is re-registered against the new dataset with the SAME
    ``agent_session_name`` (conn_uuid) + ``session_id`` (``hook.log`` records
    ``agent re-registered``);
  * a switch to the already-active dataset (or to nothing) is a no-op;
  * a launch that never switched resolves the configured dataset unchanged.

Fixture-free (sibling convention): every plugin path is redirected under a temp
home with save/restore and network calls are stubbed, so the suite is
deterministic and needs no live Cognee server. Runs under both
`python3 integrations/claude-code/tests/test_dataset_switch.py` and `pytest`.
"""

import asyncio
import contextlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _plugin_common as pc  # noqa: E402
import config  # noqa: E402


def _load_dataset_switch():
    """Import the hyphenated dataset-switch.py module by path."""
    spec = importlib.util.spec_from_file_location("dataset_switch", _SCRIPTS / "dataset-switch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _isolate(session_key="hostkey1"):
    """Redirect every plugin path under a temp home and reset COGNEE_* env."""
    pc_attrs = ("_PLUGIN_DIR", "_HOOK_LOG", "_SESSIONS_MAP_DIR", "_BRIDGE_DIR")
    cfg_attrs = ("_CONFIG_DIR", "_STATE_DIR", "_CONFIG_FILE", "_HOOK_LOG")
    saved_pc = {a: getattr(pc, a) for a in pc_attrs}
    saved_cfg = {a: getattr(config, a) for a in cfg_attrs}
    saved_env = {
        k: os.environ.get(k)
        for k in list(os.environ)
        if k.startswith("COGNEE_") or k == "CLAUDE_CWD"
    }
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp)
        plugin = home / ".cognee-plugin" / "claude-code"
        plugin.mkdir(parents=True)
        pc._PLUGIN_DIR = plugin
        pc._HOOK_LOG = plugin / "hook.log"
        pc._SESSIONS_MAP_DIR = plugin / "sessions"
        pc._BRIDGE_DIR = plugin / "bridge"
        config._CONFIG_DIR = plugin
        config._STATE_DIR = plugin
        config._CONFIG_FILE = plugin / "config.json"
        config._HOOK_LOG = plugin / "hook.log"
        for k in list(os.environ):
            if k.startswith("COGNEE_") or k == "CLAUDE_CWD":
                del os.environ[k]
        if session_key:
            os.environ["COGNEE_SESSION_KEY"] = session_key
        try:
            yield home
        finally:
            for a, v in saved_pc.items():
                setattr(pc, a, v)
            for a, v in saved_cfg.items():
                setattr(config, a, v)
            for k in list(os.environ):
                if k.startswith("COGNEE_") or k == "CLAUDE_CWD":
                    del os.environ[k]
            for k, v in saved_env.items():
                if v is not None:
                    os.environ[k] = v


@contextlib.contextmanager
def _patched(*patches):
    """Temporarily set ``(obj, name, value)`` attributes; restore on exit."""
    saved = [(obj, name, getattr(obj, name)) for (obj, name, _) in patches]
    for obj, name, value in patches:
        setattr(obj, name, value)
    try:
        yield
    finally:
        for obj, name, value in saved:
            setattr(obj, name, value)


def _events():
    if not pc._HOOK_LOG.exists():
        return []
    lines = pc._HOOK_LOG.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _wire_http(ds, register_result=(True, {"id": "conn-123"})):
    """Stub the HTTP-mode network calls; return (captured, patches)."""
    captured = {}

    def _fake_register(*, agent_session_name, session_id="", dataset_names=None, timeout=15.0):
        captured["agent_session_name"] = agent_session_name
        captured["session_id"] = session_id
        captured["dataset_names"] = dataset_names
        return register_result

    patches = [
        (ds, "http_api_ready", lambda: True),
        (ds, "register_agent_via_http", _fake_register),
        (ds, "resolved_http_endpoint_auth", lambda: ("http://local", "key")),
        (pc, "persist_session_cache_to_graph_via_http", lambda *a, **k: True),
    ]
    return captured, patches


# --- launch-scoped override --------------------------------------------------


def test_launch_dataset_override_roundtrip():
    with _isolate():
        assert pc.active_dataset_for_launch("h1") == ""
        pc.set_active_dataset_for_launch("h1", "B")
        assert pc.active_dataset_for_launch("h1") == "B"
        # keyed by host: a different launch is unaffected.
        assert pc.active_dataset_for_launch("h2") == ""
        # an empty dataset is ignored (no accidental clear).
        pc.set_active_dataset_for_launch("h1", "")
        assert pc.active_dataset_for_launch("h1") == "B"


def test_get_dataset_unchanged_when_never_switched():
    with _isolate():
        assert config.get_dataset({"dataset": "cfg_default"}) == "cfg_default"
        assert config.get_dataset({}) == "agent_sessions"


def test_get_dataset_env_when_never_switched():
    with _isolate():
        os.environ["COGNEE_PLUGIN_DATASET"] = "A"
        assert config.get_dataset(config.load_config()) == "A"


def test_get_dataset_follows_switch_over_env():
    with _isolate(session_key="hostkey1"):
        os.environ["COGNEE_PLUGIN_DATASET"] = "A"
        pc.set_active_dataset_for_launch("hostkey1", "B")
        # the mid-session switch wins over the launch-time env pin.
        assert config.get_dataset(config.load_config()) == "B"


# --- seal --------------------------------------------------------------------


def test_seal_bridge_state_flushes_and_logs():
    with _isolate():
        calls = []

        def _fake_flush(dataset, session_id, timeout=600.0):
            calls.append((dataset, session_id))
            return True

        with _patched((pc, "persist_session_cache_to_graph_via_http", _fake_flush)):
            result = pc.seal_bridge_state("A", "sess-1")

        assert calls == [("A", "sess-1")]
        assert result["sealed"] is True
        assert result["flushed"] is True
        sealed = [e for e in _events() if e["event"] == "dataset_switch_bridge_sealed"]
        assert sealed and sealed[0]["detail"]["message"] == "old bridge sealed"


# --- orchestration (HTTP mode) ----------------------------------------------


def test_switch_http_seals_reregisters_and_redirects():
    with _isolate(session_key="hostkey1"):
        os.environ["COGNEE_PLUGIN_DATASET"] = "A"  # launch dataset
        ds = _load_dataset_switch()
        captured, patches = _wire_http(ds)
        with _patched(*patches):
            result = asyncio.run(ds.switch_dataset("B", cwd=""))

        assert result["status"] == "switched"
        assert result["old_dataset"] == "A"
        assert result["new_dataset"] == "B"
        session_id = result["session_id"]
        assert session_id

        # Agent re-registered in place: same conn_uuid handle + session, new dataset.
        assert captured["session_id"] == session_id
        assert captured["dataset_names"] == ["B"]
        assert captured["agent_session_name"].startswith("conn_")

        # Redirect: a subsequent hook now resolves the NEW dataset (switch > env).
        assert config.get_dataset(config.load_config()) == "B"

        events = _events()
        by_event = {e["event"] for e in events}
        assert "dataset_switch_bridge_sealed" in by_event
        assert "dataset_switch_agent_reregistered" in by_event
        assert "dataset_switch_complete" in by_event
        reregistered = [e for e in events if e["event"] == "dataset_switch_agent_reregistered"]
        assert reregistered[0]["detail"]["message"] == "agent re-registered"


def test_switch_noop_when_same_dataset():
    with _isolate(session_key="hostkey1"):
        os.environ["COGNEE_PLUGIN_DATASET"] = "A"
        ds = _load_dataset_switch()
        captured, patches = _wire_http(ds)
        with _patched(*patches):
            result = asyncio.run(ds.switch_dataset("A", cwd=""))

        assert result["status"] == "noop"
        assert result["reason"] == "already_active"
        assert "agent_session_name" not in captured  # never re-registered


def test_switch_noop_when_no_new_dataset():
    with _isolate(session_key="hostkey1"):
        os.environ["COGNEE_PLUGIN_DATASET"] = "A"
        ds = _load_dataset_switch()
        _captured, patches = _wire_http(ds)
        with _patched(*patches):
            result = asyncio.run(ds.switch_dataset("", cwd=""))

        assert result["status"] == "noop"
        assert result["reason"] == "no_new_dataset"


def test_resolve_new_dataset_precedence():
    with _isolate():
        ds = _load_dataset_switch()
        os.environ["COGNEE_SWITCH_DATASET"] = "env-ds"
        # CLI arg wins over env and payload.
        assert (
            ds._resolve_new_dataset({"new_dataset": "payload-ds"}, ["prog", "cli-ds"]) == "cli-ds"
        )
        # env wins over payload.
        assert ds._resolve_new_dataset({"new_dataset": "payload-ds"}, ["prog"]) == "env-ds"
        del os.environ["COGNEE_SWITCH_DATASET"]
        # payload fallback, then empty.
        assert ds._resolve_new_dataset({"dataset": "payload-ds"}, ["prog"]) == "payload-ds"
        assert ds._resolve_new_dataset({}, ["prog"]) == ""


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
    sys.exit(1 if failures else 0)
