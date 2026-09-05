"""Unit tests for the COGNEE_WARMUP cold-start warmup ping (_maybe_warmup_cloud).

When COGNEE_WARMUP is truthy AND the endpoint is remote, SessionStart fires a single
non-blocking GET /health in a daemon thread to warm a scale-to-zero cloud tenant before
the first recall. These tests drive _maybe_warmup_cloud directly, with a fake
threading.Thread that records the spawn, so no live server is needed. Local endpoints
and falsey/unset flags must not spawn anything; the ping target (_health_ok) must never
raise so the daemon thread can fail silently.

Run: python integrations/claude-code/tests/test_warmup_ping.py (or via pytest).
"""

import importlib.util
import os
import pathlib
import sys
import types

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def _load():
    sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "session_start_mod", _SCRIPTS / "session-start.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ss = _load()


def _spawn_for(target_url, flag):
    """Run _maybe_warmup_cloud with COGNEE_WARMUP=flag and a recording fake Thread.

    Returns the captured Thread kwargs, or an empty dict if no thread was spawned.
    """
    captured = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            captured.update(target=target, args=args, daemon=daemon)

        def start(self):
            captured["started"] = True

    saved_threading = ss.threading
    ss.threading = types.SimpleNamespace(Thread=_FakeThread)
    if flag is None:
        os.environ.pop("COGNEE_WARMUP", None)
    else:
        os.environ["COGNEE_WARMUP"] = flag
    try:
        ss._maybe_warmup_cloud(target_url)
    finally:
        ss.threading = saved_threading
        os.environ.pop("COGNEE_WARMUP", None)
    return captured


def test_warmup_pings_remote_health_when_enabled():
    captured = _spawn_for("https://tenant.cognee.ai", "true")
    assert captured.get("started") is True
    assert captured["daemon"] is True
    assert captured["target"] is ss._health_ok
    assert captured["args"] == ("https://tenant.cognee.ai/health",)


def test_warmup_accepts_all_truthy_values():
    for flag in ("true", "TRUE", "1", "yes"):
        assert _spawn_for("https://tenant.cognee.ai", flag).get("started") is True, flag


def test_warmup_skipped_when_disabled():
    for flag in (None, "", "false", "0", "no"):
        assert _spawn_for("https://tenant.cognee.ai", flag) == {}, flag


def test_warmup_skipped_for_local_endpoint():
    # Local mode has no cold start, so the ping must not fire even with the flag on.
    for url in ("http://localhost:8011", "http://127.0.0.1:9000"):
        assert _spawn_for(url, "true") == {}, url


def test_health_ok_fails_silent_on_unreachable():
    # The daemon thread's target must swallow errors so a dead tenant never surfaces.
    assert ss._health_ok("http://127.0.0.1:1/health", timeout=0.2) is False


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
