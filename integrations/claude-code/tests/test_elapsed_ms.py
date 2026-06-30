import asyncio
import importlib.util
import pathlib
import sys
from types import SimpleNamespace

SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


pc = _load("_plugin_common", "_plugin_common.py")
scl = _load("session_context_lookup", "session-context-lookup.py")
sts = _load("store_to_session", "store-to-session.py")
iw = _load("idle_watcher", "idle-watcher.py")


def test_context_lookup_logs_elapsed_ms(monkeypatch):
    events = []
    timeline = iter([10.0, 10.2, 10.2, 10.5])

    async def fake_ensure_cognee_ready(_config):
        return None

    async def fake_resolve_user(_user_id):
        return SimpleNamespace(id="u1")

    async def fake_wait_for(awaitable, timeout=None):
        return await awaitable

    monkeypatch.setattr(scl, "load_config", lambda: {})
    monkeypatch.setattr(scl, "resolve_runtime_mode", lambda: {"mode": "local", "base_url": ""})
    monkeypatch.setattr(scl, "server_ready_hint", lambda _url: True)
    monkeypatch.setattr(scl, "ensure_cognee_ready", fake_ensure_cognee_ready)
    monkeypatch.setattr(scl, "_load_session_id", lambda: "s1")
    monkeypatch.setattr(
        scl, "read_and_reset_save_counter", lambda _session_id: {"prompt": 0, "trace": 0, "answer": 0}
    )
    monkeypatch.setattr(scl, "resolve_user", fake_resolve_user)
    monkeypatch.setattr(scl, "_recent_trace_fallback", lambda *a, **k: [])
    monkeypatch.setattr(scl, "recall_via_http", lambda *a, **k: [])
    monkeypatch.setattr(scl, "_float_env", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(scl.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(scl, "notify", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scl, "hook_log", lambda event, detail=None: events.append((event, detail)))
    monkeypatch.setattr(scl.time, "monotonic", lambda: next(timeline))

    asyncio.run(scl._run("hello world"))
    hit_or_empty = [detail for event, detail in events if event in {"context_lookup_hit", "context_lookup_empty"}]
    assert hit_or_empty and "elapsed_ms" in hit_or_empty[0]


def test_bridge_poll_logs_elapsed_ms(monkeypatch):
    events = []
    timeline = iter([0.0, 0.1, 0.2, 0.35, 0.5])

    monkeypatch.setattr(pc, "_local_api_url", lambda: "http://localhost")
    monkeypatch.setattr(pc, "_backend_reachable", lambda _url: True)
    monkeypatch.setattr(pc, "_api_key", lambda: "token")
    monkeypatch.setattr(pc, "_format_cached_bridge_document", lambda *a, **k: ("doc", ""))
    monkeypatch.setattr(pc, "_bridge_file", lambda _session_id: pathlib.Path("bridge.json"))
    monkeypatch.setattr(pc, "_load_json_file", lambda _path: {})
    monkeypatch.setattr(pc, "_write_json_file", lambda *a, **k: None)
    monkeypatch.setattr(pc, "_post_remember_document", lambda *a, **k: {"ok": True, "dataset_id": "ds1"})
    monkeypatch.setattr(pc, "wait_for_cognify", lambda *a, **k: "completed")
    monkeypatch.setattr(pc, "hook_log", lambda event, detail=None: events.append((event, detail)))
    monkeypatch.setattr(pc.time, "monotonic", lambda: next(timeline))

    assert pc.persist_session_cache_to_graph_via_http("dataset", "session") is True
    poll_events = [detail for event, detail in events if event == "http_bridge_poll"]
    assert poll_events and "elapsed_ms" in poll_events[0]


def test_improve_logs_elapsed_ms(monkeypatch):
    events = []

    monkeypatch.setattr(sts, "time", SimpleNamespace(monotonic=lambda: 1.0))
    monkeypatch.setattr(sts, "hook_log", lambda event, detail=None: events.append((event, detail)))
    monkeypatch.setattr(sts, "notify", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sts, "http_api_ready", lambda: True)
    monkeypatch.setattr(sts, "persist_session_cache_to_graph_via_http", lambda *a, **k: True)

    asyncio.run(sts._fire_improve_background("dataset", "session", None, "idle"))
    assert any(event == "auto_bridge_fired" and detail and "elapsed_ms" in detail for event, detail in events)

    events.clear()
    monkeypatch.setattr(iw, "_log", lambda event, **detail: events.append((event, detail)))
    monkeypatch.setattr(iw, "http_api_ready", lambda: True, raising=False)
    monkeypatch.setattr(iw, "persist_session_cache_to_graph_via_http", lambda *a, **k: True, raising=False)

    async def fake_improve_once(session_id, dataset, config):
        return await iw._improve_once(session_id, dataset, config)

    # Minimal happy path: the API-mode branch should log elapsed_ms.
    monkeypatch.setattr(iw, "time", SimpleNamespace(time=lambda: 1.0, sleep=lambda *_args, **_kwargs: None))
    monkeypatch.setattr(iw, "_PLUGIN_DIR", pathlib.Path("."))
    monkeypatch.setattr(iw, "_STOPFILE", pathlib.Path("does-not-exist"))
    monkeypatch.setattr(iw, "_owns_pidfile", lambda: False)

    asyncio.run(iw._improve_once("session", "dataset", {}))
    assert any(event == "session_bridge_done" and detail and "elapsed_ms" in detail for event, detail in events)
