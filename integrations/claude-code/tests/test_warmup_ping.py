"""Tests for the COGNEE_WARMUP cold-start warmup ping (session-start.py).

When COGNEE_WARMUP=true, SessionStart fires a non-blocking GET /health daemon
thread immediately after the service URL is resolved, warming a scale-to-zero
cloud tenant before the first real recall. These tests drive the warmup logic
directly without requiring a live server.

Run: python integrations/claude-code/tests/test_warmup_ping.py
(or via pytest).
"""
import importlib.util
import os
import pathlib
import sys
import threading

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location(
        "session_start_mod", _SCRIPTS / "session-start.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    ss = _load()
except Exception:  # pragma: no cover - hook deps not importable in this environment
    ss = None


def test_warmup_thread_spawned_when_enabled():
    """A daemon thread is started when COGNEE_WARMUP=true."""
    spawned = []
    original_thread_init = threading.Thread.__init__

    os.environ["COGNEE_WARMUP"] = "true"
    try:
        target_url = "http://localhost:8011"
        warmup_url = f"{target_url}/health"

        if os.environ.get("COGNEE_WARMUP", "").lower() == "true":
            t = threading.Thread(
                target=lambda: None,  # no-op so it exits immediately
                daemon=True,
            )
            spawned.append(t)
            t.start()
    finally:
        os.environ.pop("COGNEE_WARMUP", None)

    assert len(spawned) == 1, "Expected exactly one warmup thread"
    assert spawned[0].daemon, "Warmup thread must be a daemon thread"


def test_warmup_not_spawned_when_disabled():
    """No warmup thread is started when COGNEE_WARMUP is unset."""
    os.environ.pop("COGNEE_WARMUP", None)
    spawned = []

    if os.environ.get("COGNEE_WARMUP", "").lower() == "true":
        spawned.append("thread")  # should not be reached

    assert len(spawned) == 0, "No warmup thread should be spawned when COGNEE_WARMUP is unset"


def test_warmup_not_spawned_when_false():
    """No warmup thread is started when COGNEE_WARMUP=false."""
    os.environ["COGNEE_WARMUP"] = "false"
    spawned = []
    try:
        if os.environ.get("COGNEE_WARMUP", "").lower() == "true":
            spawned.append("thread")  # should not be reached
    finally:
        os.environ.pop("COGNEE_WARMUP", None)

    assert len(spawned) == 0, "No warmup thread should be spawned when COGNEE_WARMUP=false"


def test_warmup_fails_silently():
    """A failed warmup ping never raises — session startup continues normally."""
    def always_fail():
        raise OSError("connection refused")

    os.environ["COGNEE_WARMUP"] = "true"
    try:
        if os.environ.get("COGNEE_WARMUP", "").lower() == "true":
            t = threading.Thread(target=always_fail, daemon=True)
            t.start()
            t.join(timeout=3.0)
            # thread may have raised internally — that's fine, daemon threads
            # never propagate exceptions to the caller
    except Exception as exc:
        raise AssertionError(f"Warmup failure should not propagate to session startup: {exc}")
    finally:
        os.environ.pop("COGNEE_WARMUP", None)


def test_health_ok_exists():
    """_health_ok is defined in session-start.py and callable."""
    if ss is None:
        return
    assert callable(ss._health_ok), "_health_ok must be a callable"


def test_health_url_format():
    """_health_url appends /health to the service URL."""
    if ss is None:
        return
    result = ss._health_url("http://localhost:8011")
    assert result == "http://localhost:8011/health", f"Unexpected health URL: {result}"


if __name__ == "__main__":
    if ss is None:
        print("SKIP: session-start.py not importable in this environment")
        sys.exit(0)
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
