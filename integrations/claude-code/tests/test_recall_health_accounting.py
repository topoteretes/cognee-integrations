"""Tests for the hot-path health accounting in session-context-lookup._run (SDK-356).

The recall attempt IS the probe: each prompt's scope calls are folded back into
the shared connection state. Under test:

  * 401/403 → "auth_failed" written, remaining scopes skipped (shared key —
    they are doomed to the same rejection). This restores the auth detection
    the old pre-recall authed probe provided, from a real request.
  * 5xx on every answered scope → "server_error" written + one breaker failure.
  * connection refused → "unreachable" written (with warm-up suppression when
    there is no prior ready marker) and remaining scopes skipped.
  * all-timeout prompt → nothing written below the streak threshold; "slow"
    written at the threshold.
  * success → marker refreshed (when stale) and the breaker cleared.

Follows the test_per_scope_timing conventions: the hyphenated hook module is
loaded via importlib, seams are stubbed as module attributes, a fake
_cognee_client is injected, and HOME is redirected for the best-effort
last_recall writes.

Run: python integrations/claude-code/tests/test_recall_health_accounting.py
(or via pytest).
"""

import asyncio
import importlib.util
import os
import pathlib
import sys
import tempfile
import types
import urllib.error

os.environ.setdefault("COGNEE_PLUGIN_IN_VENV", "1")

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

_URL = "https://cloud.example"


def _load_hook_module():
    path = SCRIPTS / "session-context-lookup.py"
    spec = importlib.util.spec_from_file_location("session_context_lookup_health", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drive(recall_fn, *, prior=None, ready_hint=False, streak=1, threshold=3):
    """Run _run in cloud mode with all state seams captured.

    Returns (events, state_writes, breaker_calls, recall_calls).
    """
    mod = _load_hook_module()
    events, writes, breaker_calls, calls = [], [], [], []

    def _recall(prompt, **kw):
        calls.append(kw["scope"][0])
        return recall_fn(prompt, **kw)

    mod.hook_log = lambda ev, detail=None: events.append((ev, detail or {}))
    mod.notify = lambda *a, **k: None
    mod.load_config = lambda: {}
    mod.resolve_runtime_mode = lambda: {"mode": "http", "base_url": _URL}
    mod.read_connection_state = lambda: dict(prior or {})
    mod.server_ready_hint = lambda url: ready_hint
    mod.mark_server_ready = lambda url: writes.append(("ready", url, ""))
    mod.write_connection_state = lambda state, url, detail="": writes.append((state, url, detail))
    mod.clear_slow_streak = lambda url: None
    mod.record_slow_probe = lambda url: streak
    mod.slow_streak_threshold = lambda: threshold
    mod._load_session_id = lambda: "sid"
    mod.read_and_reset_save_counter = lambda sid: {"prompt": 0, "trace": 0, "answer": 0}
    mod.recall_via_http = _recall

    fake_client = types.ModuleType("_cognee_client")
    fake_client.breaker_open = lambda service_url="": (False, 0)
    fake_client.record_success = lambda service_url="": breaker_calls.append(
        ("success", service_url)
    )
    fake_client.record_failure = lambda error="", now=None, service_url="", reason="": (
        breaker_calls.append(("failure", service_url, reason))
    )
    saved_client = sys.modules.get("_cognee_client")
    sys.modules["_cognee_client"] = fake_client

    saved_env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = home
        os.environ["USERPROFILE"] = home
        try:
            asyncio.run(mod._run("please recall something relevant"))
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            if saved_client is None:
                sys.modules.pop("_cognee_client", None)
            else:
                sys.modules["_cognee_client"] = saved_client
    return events, writes, breaker_calls, calls


def _raiser(exc_factory):
    def _fn(prompt, **kw):
        raise exc_factory()

    return _fn


def _http_error(code):
    return urllib.error.HTTPError(_URL, code, "boom", {}, None)


_READY_PRIOR = {"state": "ready", "base_url": _URL, "checked_at": 1.0}


# ── auth failure: detected from the real request, budget not wasted ─────────


def test_401_writes_auth_failed_and_skips_remaining_scopes():
    events, writes, _breaker, calls = _drive(_raiser(lambda: _http_error(401)))
    assert ("auth_failed", _URL, "401/403 during recall") in writes
    assert len(calls) == 1, f"remaining scopes must be skipped, got {calls}"
    assert any(ev == "recall_auth_rejected" for ev, _ in events)


def test_403_also_writes_auth_failed():
    _events, writes, _breaker, _calls = _drive(_raiser(lambda: _http_error(403)))
    assert any(w[0] == "auth_failed" for w in writes)


# ── 5xx: reachable but failing ───────────────────────────────────────────────


def test_5xx_on_all_scopes_writes_server_error_and_one_breaker_failure():
    _events, writes, breaker, calls = _drive(_raiser(lambda: _http_error(503)))
    assert ("server_error", _URL, "5xx during recall") in writes
    assert breaker == [("failure", _URL, "server_error")], breaker
    assert len(calls) == 4, "5xx is not a shared-key verdict — all scopes still try"


# ── refused: definitive down, warming-suppressed on cold start ───────────────


def test_refused_with_prior_ready_writes_unreachable_and_stops():
    def _refused():
        return urllib.error.URLError(ConnectionRefusedError(61, "refused"))

    _events, writes, breaker, calls = _drive(_raiser(_refused), prior=_READY_PRIOR)
    assert any(w[0] == "unreachable" for w in writes)
    assert ("failure", _URL, "unreachable") in breaker
    assert len(calls) == 1, f"remaining scopes must be skipped, got {calls}"


def test_refused_without_prior_ready_is_warming_and_writes_nothing():
    def _refused():
        return urllib.error.URLError(ConnectionRefusedError(61, "refused"))

    _events, writes, breaker, _calls = _drive(_raiser(_refused), prior=None)
    assert writes == [], writes
    assert breaker == [], breaker


# ── timeouts: no verdict until the streak threshold ──────────────────────────


def test_all_timeout_below_threshold_writes_nothing():
    _events, writes, breaker, calls = _drive(
        _raiser(lambda: TimeoutError("timed out")), streak=1, threshold=3
    )
    assert writes == [], writes
    assert breaker == [], "timeouts must not feed the breaker"
    assert len(calls) == 4


def test_all_timeout_at_threshold_writes_not_responding():
    """N consecutive timeout-only prompts escalate — but never to "unreachable":
    the server exists (nothing refused), it just isn't answering."""
    events, writes, breaker, _calls = _drive(
        _raiser(lambda: TimeoutError("timed out")), streak=3, threshold=3
    )
    assert any(w[0] == "not_responding" for w in writes), writes
    assert not any(w[0] == "unreachable" for w in writes), writes
    assert breaker == [], "timeouts must not feed the breaker even at threshold"
    assert any(ev == "slow_streak_escalated" for ev, _ in events)


# ── success: the attempt is the ready probe ──────────────────────────────────


def test_success_refreshes_stale_marker_and_clears_breaker():
    _events, writes, breaker, _calls = _drive(lambda prompt, **kw: [], ready_hint=False)
    assert ("ready", _URL, "") in writes
    assert ("success", _URL) in breaker


def test_success_with_fresh_marker_does_not_rewrite():
    _events, writes, _breaker, _calls = _drive(lambda prompt, **kw: [], ready_hint=True)
    assert writes == [], writes


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
