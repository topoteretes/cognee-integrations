"""Unit tests for per-operation timeout env vars in _plugin_common.py.

remember/register each read their own timeout env var
(`COGNEE_REMEMBER_TIMEOUT` / `COGNEE_REGISTER_TIMEOUT`) with a sane default,
tunable independently of recall. An explicit ``timeout`` argument still wins,
and a malformed env value falls back to the default.

Run: `pytest integrations/claude-code/tests/test_timeout_env.py`
(or `python integrations/claude-code/tests/test_timeout_env.py` standalone).
"""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402


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


# Recall keeps its own independent env var (COGNEE_RECALL_TIMEOUT, in
# _cognee_client.py) — unaffected by the two added above.


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
