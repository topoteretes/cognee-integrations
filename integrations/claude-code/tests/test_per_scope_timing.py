"""Unit tests for per-scope recall instrumentation (session-context-lookup.py).

Recall fans out over 5 scopes (session/trace/graph_context/graph/session_context).
This test drives the hook's ``_run`` in cloud mode with a fake ``recall_via_http``
and asserts the emitted recall event carries a per-scope ``{hits, elapsed_ms}``
breakdown for all 5 scopes — including scopes that return nothing — without
changing the existing aggregate ``counts``.

Run: `pytest integrations/claude-code/tests/test_per_scope_timing.py -v`
(or `python integrations/claude-code/tests/test_per_scope_timing.py` standalone).
"""

import asyncio
import importlib.util
import os
import pathlib
import sys
import types

# Pin the loop-guard so importing the plugin never re-execs into its venv.
os.environ.setdefault("COGNEE_PLUGIN_IN_VENV", "1")

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

# The circuit breaker is imported lazily inside _run (cloud mode). Inject a
# fake so the test is deterministic regardless of any on-disk breaker state.
_fake_client = types.ModuleType("_cognee_client")
_fake_client.breaker_open = lambda: (False, 0)
sys.modules["_cognee_client"] = _fake_client


def _load_hook_module():
    """Import the hyphenated hook script under a clean module name."""
    path = SCRIPTS / "session-context-lookup.py"
    spec = importlib.util.spec_from_file_location("session_context_lookup", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SCOPES = ["session", "trace", "graph_context", "graph", "session_context"]


def _run_with_recall(mod, monkeypatch, tmp_home, recall_map):
    """Drive _run in cloud mode with a fake recall, returning captured events."""
    events: list[tuple[str, dict]] = []

    def _fake_recall(prompt, **kwargs):
        return list(recall_map.get(kwargs["scope"][0], []))

    monkeypatch.setattr(mod, "hook_log", lambda ev, detail=None: events.append((ev, detail or {})))
    monkeypatch.setattr(mod, "notify", lambda *a, **k: None)
    monkeypatch.setattr(mod, "resolve_runtime_mode",
                        lambda: {"mode": "http", "base_url": "https://cloud.example"})
    monkeypatch.setattr(mod, "server_ready_hint", lambda _url: True)
    monkeypatch.setattr(mod, "_load_session_id", lambda: "sess-123")
    monkeypatch.setattr(mod, "read_and_reset_save_counter",
                        lambda _sid: {"prompt": 0, "trace": 0, "answer": 0})
    monkeypatch.setattr(mod, "recall_via_http", _fake_recall)
    # Redirect last_recall.json / recall-audit.log writes into a tmp HOME.
    monkeypatch.setenv("HOME", str(tmp_home))
    monkeypatch.setenv("USERPROFILE", str(tmp_home))

    output = asyncio.run(mod._run("please recall something relevant"))
    return output, events


def _event(events, name):
    for ev, detail in events:
        if ev == name:
            return detail
    return None


def _assert_valid_per_scope(per_scope):
    assert list(per_scope.keys()) == SCOPES, f"expected all 5 scopes in order, got {per_scope}"
    for label, rec in per_scope.items():
        assert isinstance(rec["hits"], int), f"{label} hits not int: {rec}"
        assert isinstance(rec["elapsed_ms"], (int, float)), f"{label} elapsed not numeric: {rec}"
        assert rec["elapsed_ms"] >= 0


def test_per_scope_on_hit(monkeypatch, tmp_path):
    """A hit emits context_lookup_hit carrying per-scope hits + timing."""
    mod = _load_hook_module()
    recall_map = {
        "session": [{"question": "q1", "answer": "a1"}],
        "trace": [],
        "graph_context": [{"source": "graph_context", "content": "ctx"}],
        "graph": [{"source": "graph", "content": "gg"}],
        "session_context": [],
    }
    _out, events = _run_with_recall(mod, monkeypatch, tmp_path, recall_map)

    detail = _event(events, "context_lookup_hit")
    assert detail is not None, "expected a context_lookup_hit event"
    assert "counts" in detail, "existing aggregate counts must be preserved"
    per_scope = detail["per_scope"]
    _assert_valid_per_scope(per_scope)
    # Raw per-scope hit attribution (pre-bucketing): graph is NOT folded here.
    assert per_scope["session"]["hits"] == 1
    assert per_scope["trace"]["hits"] == 0
    assert per_scope["graph_context"]["hits"] == 1
    assert per_scope["graph"]["hits"] == 1
    assert per_scope["session_context"]["hits"] == 0


def test_per_scope_on_empty(monkeypatch, tmp_path):
    """A total miss still emits per-scope timing for all 5 scopes."""
    mod = _load_hook_module()
    recall_map = {s: [] for s in SCOPES}
    _out, events = _run_with_recall(mod, monkeypatch, tmp_path, recall_map)

    detail = _event(events, "context_lookup_empty")
    assert detail is not None, "expected a context_lookup_empty event"
    per_scope = detail["per_scope"]
    _assert_valid_per_scope(per_scope)
    assert all(rec["hits"] == 0 for rec in per_scope.values())


def _run_all():
    """Standalone runner (no pytest) using a minimal monkeypatch shim."""
    class _MP:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        def setenv(self, name, val):
            self._undo.append((os.environ, name, os.environ.get(name)))
            os.environ[name] = val

        def undo(self):
            for obj, name, old in reversed(self._undo):
                if obj is os.environ:
                    if old is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = old
                else:
                    setattr(obj, name, old)

    import tempfile

    green, red, bold, reset = "\033[32m", "\033[31m", "\033[1m", "\033[0m"
    failures = 0
    for test in (test_per_scope_on_hit, test_per_scope_on_empty):
        mp = _MP()
        try:
            with tempfile.TemporaryDirectory() as td:
                test(mp, pathlib.Path(td))
            print(f"{green}PASS{reset}  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"{red}FAIL{reset}  {test.__name__}: {exc}")
        finally:
            mp.undo()
    if failures:
        print(f"{red}{bold}{failures} FAILED{reset}")
        sys.exit(1)
    print(f"{green}{bold}2 passed{reset}")


if __name__ == "__main__":
    _run_all()
