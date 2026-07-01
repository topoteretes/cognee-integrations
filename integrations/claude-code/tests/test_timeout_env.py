"""Unit tests for per-operation timeout env vars.

remember/register each read their own timeout env var
(`COGNEE_REMEMBER_TIMEOUT` / `COGNEE_REGISTER_TIMEOUT`) with a sane default,
tunable independently of recall. An explicit ``timeout`` argument still wins,
and a malformed env value falls back to the default.

`COGNEE_REMEMBER_TIMEOUT` covers BOTH remember paths: the cached-entry write in
`_plugin_common.remember_entry_via_http` (default 30s) and the server POST in
`_remember_http.do_remember` (default 60s, preserving prior behavior).

Run: `pytest integrations/claude-code/tests/test_timeout_env.py`
(or `python integrations/claude-code/tests/test_timeout_env.py` standalone).
"""

import os
import pathlib
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402
import _remember_http as rh  # noqa: E402


def _capture_timeout():
    """Patch the HTTP layer to record the timeout it is called with."""
    captured = {}

    def fake(path, payload=None, *, method="POST", timeout=None):
        captured["timeout"] = timeout
        return {}

    pc._json_http_request = fake
    return captured


def _clear_env():
    for k in ("COGNEE_REMEMBER_TIMEOUT", "COGNEE_REGISTER_TIMEOUT"):
        os.environ.pop(k, None)


# --- remember ---------------------------------------------------------------
def test_remember_default_timeout():
    _clear_env()
    cap = _capture_timeout()
    pc.remember_entry_via_http("ds", "sid", {"x": 1})
    assert cap["timeout"] == 30.0


def test_remember_env_timeout():
    _clear_env()
    os.environ["COGNEE_REMEMBER_TIMEOUT"] = "7.5"
    cap = _capture_timeout()
    pc.remember_entry_via_http("ds", "sid", {"x": 1})
    assert cap["timeout"] == 7.5


def test_remember_explicit_arg_overrides_env():
    _clear_env()
    os.environ["COGNEE_REMEMBER_TIMEOUT"] = "7.5"
    cap = _capture_timeout()
    pc.remember_entry_via_http("ds", "sid", {"x": 1}, timeout=99.0)
    assert cap["timeout"] == 99.0


def test_remember_malformed_env_falls_back():
    _clear_env()
    os.environ["COGNEE_REMEMBER_TIMEOUT"] = "not-a-number"
    cap = _capture_timeout()
    pc.remember_entry_via_http("ds", "sid", {"x": 1})
    assert cap["timeout"] == 30.0


# --- register ---------------------------------------------------------------
def test_register_default_timeout():
    _clear_env()
    cap = _capture_timeout()
    pc.register_agent_via_http(agent_session_name="agent")
    assert cap["timeout"] == 15.0


def test_register_env_timeout():
    _clear_env()
    os.environ["COGNEE_REGISTER_TIMEOUT"] = "3"
    cap = _capture_timeout()
    pc.register_agent_via_http(agent_session_name="agent")
    assert cap["timeout"] == 3.0


def test_register_explicit_arg_overrides_env():
    _clear_env()
    os.environ["COGNEE_REGISTER_TIMEOUT"] = "3"
    cap = _capture_timeout()
    pc.register_agent_via_http(agent_session_name="agent", timeout=42.0)
    assert cap["timeout"] == 42.0


# --- remember (server POST path: _remember_http.do_remember) ----------------
# The actual /api/v1/remember write. Historically hardcoded to 60s; now honors
# COGNEE_REMEMBER_TIMEOUT while preserving the 60s default when unset.
def _capture_do_remember_timeout():
    """A fake opener that records the timeout, then bails so do_remember returns."""
    captured = {}

    def fake_opener(req, timeout=None):
        captured["timeout"] = timeout
        raise urllib.error.URLError("stop-after-capture")

    return captured, fake_opener


def _run_do_remember(opener, **kw):
    return rh.do_remember("http://x", "", "content", "ds", "ns", opener=opener, **kw)


def test_do_remember_default_timeout():
    _clear_env()
    cap, opener = _capture_do_remember_timeout()
    _run_do_remember(opener)
    assert cap["timeout"] == 60.0


def test_do_remember_env_timeout():
    _clear_env()
    os.environ["COGNEE_REMEMBER_TIMEOUT"] = "12.5"
    cap, opener = _capture_do_remember_timeout()
    _run_do_remember(opener)
    assert cap["timeout"] == 12.5


def test_do_remember_explicit_arg_overrides_env():
    _clear_env()
    os.environ["COGNEE_REMEMBER_TIMEOUT"] = "12.5"
    cap, opener = _capture_do_remember_timeout()
    _run_do_remember(opener, timeout=88.0)
    assert cap["timeout"] == 88.0


def test_do_remember_malformed_env_falls_back():
    _clear_env()
    os.environ["COGNEE_REMEMBER_TIMEOUT"] = "not-a-number"
    cap, opener = _capture_do_remember_timeout()
    _run_do_remember(opener)
    assert cap["timeout"] == 60.0


# Recall keeps its own independent env var (COGNEE_RECALL_TIMEOUT, in
# _cognee_client.py) — unaffected by the vars added above.


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    raise SystemExit(1 if failures else 0)
