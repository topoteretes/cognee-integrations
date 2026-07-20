"""Regression tests for latency-sensitive Codex persistence hooks."""

import asyncio
import importlib.util
import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

_SPEC = importlib.util.spec_from_file_location("store_to_session", _SCRIPTS / "store-to-session.py")
store = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(store)


def test_http_stop_buffers_without_remote_session_lookup(monkeypatch):
    buffered = []
    monkeypatch.setattr(store, "load_config", lambda: {})
    monkeypatch.setattr(store, "get_session_id", lambda _config: "session")
    monkeypatch.setattr(store, "get_dataset", lambda _config: "dataset")
    monkeypatch.setattr(
        store,
        "load_resolved",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected remote lookup")),
    )
    monkeypatch.setattr(
        store, "resolve_runtime_mode", lambda: {"mode": "http", "base_url": "https://example"}
    )
    monkeypatch.setattr(
        store,
        "pop_pending_prompt",
        lambda *_args, **_kwargs: {"prompt": "question", "context": ""},
    )
    monkeypatch.setattr(store, "server_ready_hint", lambda _url: False)
    monkeypatch.setattr(store, "append_warmup_entry", lambda *args: buffered.append(args))
    monkeypatch.setattr(store, "append_http_bridge_entry", lambda *args, **kwargs: None)
    monkeypatch.setattr(store, "bump_save_counter", lambda *args: None)
    monkeypatch.setattr(store, "hook_log", lambda *args: None)

    asyncio.run(store._store_assistant_stop({"assistant_message": "answer"}))

    assert buffered[0][:2] == ("dataset", "session")
