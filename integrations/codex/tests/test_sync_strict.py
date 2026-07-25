"""Unit tests for sync-session-to-graph.py strict mode.

The detached final-sync worker retries only on exceptions. Strict mode (used
by that worker) makes an incomplete session sync raise so the retry loop
re-drives the whole drain+improve, instead of silently reporting success on
the session's LAST sync. Non-strict (manual /cognee-sync, mid-session) keeps
the old log-and-return behavior.

Run: python integrations/codex/tests/test_sync_strict.py (or via pytest).
"""

import asyncio
import importlib.util
import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

_spec = importlib.util.spec_from_file_location("sync_mod", _SCRIPTS / "sync-session-to-graph.py")
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _stub(wrote, *, unregister_calls=None):
    """Patch the module seams for one _sync run; return a restore fn."""
    saved = {
        k: getattr(m, k)
        for k in (
            "_load_resolved",
            "load_config",
            "http_api_ready",
            "run_session_improve",
            "unregister_agent_via_http",
            "hook_log",
        )
    }
    # (session_id, dataset, user_id, agent_session_name, was_registered, has_api_key, session_key)
    m._load_resolved = lambda: ("sess1", "ds", "u1", "agent1", True, True, "key1")
    m.load_config = lambda: {}
    m.http_api_ready = lambda: True
    m.run_session_improve = lambda d, s: wrote
    m.unregister_agent_via_http = lambda **k: (
        (unregister_calls.append(k) if unregister_calls is not None else None) or (True, 0)
    )
    m.hook_log = lambda *a, **k: None

    def _restore():
        for k, v in saved.items():
            setattr(m, k, v)

    return _restore


def test_strict_raises_on_incomplete_sync():
    restore = _stub(wrote=False)
    try:
        try:
            asyncio.run(m._sync(stop_watcher=False, strict=True))
            raise AssertionError("expected RuntimeError for incomplete strict sync")
        except RuntimeError as exc:
            assert "incomplete" in str(exc)
    finally:
        restore()


def test_non_strict_does_not_raise():
    restore = _stub(wrote=False)
    try:
        asyncio.run(m._sync(stop_watcher=False, strict=False))  # must not raise
    finally:
        restore()


def test_strict_complete_sync_does_not_raise():
    restore = _stub(wrote=True)
    try:
        asyncio.run(m._sync(stop_watcher=False, strict=True))  # must not raise
    finally:
        restore()


def test_unregister_still_runs_when_strict_raises():
    # The finally-block unregister must run even when strict mode raises, so a
    # retried worker never leaves a dangling registration.
    calls = []
    restore = _stub(wrote=False, unregister_calls=calls)
    try:
        try:
            asyncio.run(m._sync(stop_watcher=False, unregister_on_finish=True, strict=True))
        except RuntimeError:
            pass
        assert len(calls) == 1
        assert calls[0].get("agent_session_name") == "agent1"
    finally:
        restore()


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
