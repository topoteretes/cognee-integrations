"""Regression tests for the exit-watcher's session-long credits refresh.

The exit-watcher is the only plugin process whose lifetime matches the host
session — the idle watcher exits at ``bridge_complete`` minutes after the
last activity, which is exactly why a credits poll there let the status-line
segment age out of its 15-minute TTL during longer idle stretches ("credits
disappeared after the terminal was open for a while"). These pin the gate:
cloud URL (bootstrap value or env) → refresh; local/none → skip; fresh
tenant entry → throttled.

Run: python integrations/claude-code/tests/test_exit_watcher_credits.py (or via pytest).
"""

import importlib.util
import os
import pathlib
import sys

os.environ.setdefault("COGNEE_PLUGIN_IN_VENV", "1")

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _plugin_common as pc  # noqa: E402

_CLOUD_URL = "https://tenant-f8c21da4-6674-4cc5-bc56-de5e93db881d.aws.cognee.ai"


def _load_watcher():
    spec = importlib.util.spec_from_file_location("exit_watcher_test", _SCRIPTS / "exit-watcher.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drive(fn, *, service_url, env_url=None, marker=None):
    mod = _load_watcher()
    mod._log = lambda *a, **k: None
    saved = {
        k: getattr(pc, k) for k in ("refresh_credits", "read_credits_marker", "_local_api_url")
    }
    saved_env = {k: os.environ.get(k) for k in ("COGNEE_BASE_URL", "COGNEE_LOCAL_API_URL")}
    os.environ.pop("COGNEE_LOCAL_API_URL", None)
    if env_url is None:
        os.environ.pop("COGNEE_BASE_URL", None)
    else:
        os.environ["COGNEE_BASE_URL"] = env_url
    calls = {"refreshes": 0}
    pc.refresh_credits = lambda *a, **k: calls.update(refreshes=calls["refreshes"] + 1) or {}
    pc.read_credits_marker = lambda: marker or {}
    try:
        mod._refresh_credits_marker(service_url)
        return fn(calls)
    finally:
        for k, v in saved.items():
            setattr(pc, k, v)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_cloud_bootstrap_url_refreshes():
    def _t(calls):
        assert calls["refreshes"] == 1

    _drive(_t, service_url=_CLOUD_URL)


def test_empty_bootstrap_falls_back_to_env():
    def _t(calls):
        assert calls["refreshes"] == 1

    _drive(_t, service_url="", env_url=_CLOUD_URL)


def test_local_bootstrap_skips():
    def _t(calls):
        assert calls["refreshes"] == 0

    _drive(_t, service_url="http://localhost:8011")


def test_no_url_anywhere_skips():
    def _t(calls):
        assert calls["refreshes"] == 0

    _drive(_t, service_url="")


def test_fresh_tenant_entry_throttles():
    import time as _time

    fresh = {
        "f8c21da4-6674-4cc5-bc56-de5e93db881d": {
            "base_url": _CLOUD_URL,
            "checked_at": _time.time(),
        }
    }

    def _t(calls):
        assert calls["refreshes"] == 0

    _drive(_t, service_url=_CLOUD_URL, marker=fresh)


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
