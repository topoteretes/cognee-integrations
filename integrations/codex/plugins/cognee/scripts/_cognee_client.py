#!/usr/bin/env python3
"""Cohesive, resilient cognee recall client for the plugin.

This is the single layer the recall paths route through — the explicit
`cognee-search.sh` wrapper and (via a shared breaker) the auto-recall hook — so a
repeatedly-failing backend trips one **circuit breaker** instead of being hammered
on every call, and every call gets a bounded, named **timeout**.

The recall transport itself lives in `_recall_http.do_recall` (server-first, with
the list / error-envelope / UNREACHABLE contract); this module adds the breaker +
timeout policy around it.

The breaker is **file-based** on purpose: each plugin hook/script runs as a
short-lived process, so in-memory state (as a long-lived provider like Hermes
uses) would not survive between calls. State lives in the plugin state dir.
"""

import json
import os
import pathlib
import random
import sys
import time
import urllib.error

from _recall_http import UNREACHABLE, _error, do_recall

# Tunables (mirror Hermes's provider defaults).
_THRESHOLD = int(os.environ.get("COGNEE_BREAKER_THRESHOLD", "5"))
_COOLDOWN = float(os.environ.get("COGNEE_BREAKER_COOLDOWN", "120"))
_RECALL_TIMEOUT = float(os.environ.get("COGNEE_RECALL_TIMEOUT", "20"))

_COLDSTART_SEEN_MAX = 256  # bound the marker file's session list


def coldstart_config() -> tuple[int, float]:
    """(extra_retries, base_backoff_seconds) for the first-recall cold-start path.

    Read live so runtime/test env changes take effect. Named ``COLDSTART`` to
    signal these apply only to the first recall of a session, not every recall.
    """
    try:
        retries = int(os.environ.get("COGNEE_RECALL_COLDSTART_RETRIES", "2"))
    except (TypeError, ValueError):
        retries = 2
    try:
        backoff = float(os.environ.get("COGNEE_RECALL_COLDSTART_BACKOFF", "0.5"))
    except (TypeError, ValueError):
        backoff = 0.5
    return max(0, retries), max(0.0, backoff)


def _coldstart_state_path() -> pathlib.Path:
    base = os.environ.get("COGNEE_PLUGIN_STATE_DIR") or os.path.expanduser("~/.cognee-plugin")
    return pathlib.Path(base) / "recall-coldstart.json"


def _coldstart_seen() -> list:
    try:
        data = json.loads(_coldstart_state_path().read_text(encoding="utf-8"))
        seen = data.get("seen") if isinstance(data, dict) else None
        return seen if isinstance(seen, list) else []
    except Exception:
        return []


def is_first_recall(session_id: str) -> bool:
    """True until ``mark_recall_seen`` records this session (unknown -> treat as first)."""
    if not session_id:
        return False
    return session_id not in _coldstart_seen()


def mark_recall_seen(session_id: str) -> None:
    """Record that this session's first recall has resolved (success or exhausted)."""
    if not session_id:
        return
    seen = _coldstart_seen()
    if session_id in seen:
        return
    seen.append(session_id)
    if len(seen) > _COLDSTART_SEEN_MAX:
        seen = seen[-_COLDSTART_SEEN_MAX:]
    try:
        path = _coldstart_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"seen": seen}), encoding="utf-8")
    except Exception:
        pass


def retry_cold_start(
    attempt,
    *,
    retries: int,
    backoff: float,
    deadline=None,
    sleep=None,
    rng=None,
    monotonic=None,
):
    """Retry ``attempt`` on a cold-start miss, with jittered exponential backoff."""
    _sleep = sleep or time.sleep
    _rand = rng or random.random
    _now = monotonic or time.monotonic
    ok, value = attempt()
    tries = 0
    while not ok and tries < retries:
        if deadline is not None and (deadline - _now()) <= 0:
            break
        delay = backoff * (2**tries) * (0.5 + _rand())  # jitter in [0.5, 1.5)
        if deadline is not None:
            delay = min(delay, max(0.0, deadline - _now()))
        if delay > 0:
            _sleep(delay)
        ok, value = attempt()
        tries += 1
    return ok, value


def coldstart_recall_attempt(do_call, on_retry=None):
    """Adapt a *raising* recall call into a ``retry_cold_start`` attempt.

    Returns ``(ok, value)``: a reachable-but-rejected response (``HTTPError``)
    fails fast (re-raised, never retried); a timeout / connection error yields
    ``(False, [])`` so ``retry_cold_start`` retries. ``on_retry``, if given, is
    called with the caught exception before each retry.
    """

    def _attempt():
        try:
            return True, do_call()
        except urllib.error.HTTPError:
            raise
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            if on_retry is not None:
                on_retry(exc)
            return False, []

    return _attempt


def _state_path():
    base = os.environ.get("COGNEE_PLUGIN_STATE_DIR") or os.path.expanduser("~/.cognee-plugin")
    return pathlib.Path(base) / "recall-breaker.json"


def _read():
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(state):
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


def breaker_open(now=None):
    """Return (is_open, retry_in_seconds). Open while we're inside the cooldown window."""
    now = time.time() if now is None else now
    until = float(_read().get("cooldown_until") or 0.0)
    return (True, int(until - now)) if now < until else (False, 0)


def record_failure(error="", now=None):
    """Count a backend failure; open the breaker once we hit the threshold."""
    now = time.time() if now is None else now
    state = _read()
    failures = int(state.get("failures") or 0) + 1
    state["failures"] = failures
    state["last_error"] = str(error)[:200]
    if failures >= _THRESHOLD:
        state["cooldown_until"] = now + _COOLDOWN
    _write(state)


def record_success():
    """Backend answered — clear the breaker."""
    _write({"failures": 0, "cooldown_until": 0.0})


def recall(service_url, api_key, query, session_id, scope, top_k, dataset="", *, timeout=None):
    """Breaker-wrapped recall. Returns a list, an error-envelope dict, or UNREACHABLE.

    Only genuine backend trouble trips the breaker: UNREACHABLE (connection
    failure) or a 5xx. A reachable 4xx (e.g. 401/403 auth) is a config problem —
    surfaced, but it does NOT open the breaker (waiting wouldn't fix it).
    """
    is_open, retry = breaker_open()
    if is_open:
        # We're in cooldown: surface a clear message and do NOT call (and, since
        # this isn't UNREACHABLE, the wrapper won't fall back to the CLI either —
        # which would just hammer the same down server).
        return _error(503, "cognee temporarily unavailable (circuit open, retry in ~%ds)" % retry)

    _timeout = timeout or _RECALL_TIMEOUT

    def _once():
        return do_recall(
            service_url, api_key, query, session_id, scope, top_k, dataset, timeout=_timeout
        )

    if session_id and is_first_recall(session_id):

        def _attempt():
            r = _once()
            return (r != UNREACHABLE, r)  # ok unless the server was unreachable

        retries, backoff = coldstart_config()
        _ok, result = retry_cold_start(_attempt, retries=retries, backoff=backoff)
        mark_recall_seen(session_id)
    else:
        result = _once()

    if result == UNREACHABLE:
        record_failure("unreachable")
    elif isinstance(result, dict) and int(result.get("status") or 0) >= 500:
        record_failure("http %s" % result.get("status"))
    else:
        record_success()
    return result


def main(argv):
    # argv: service_url, api_key, query, session_id, scope, top_k[, dataset]
    a = list(argv) + [""] * 7
    result = recall(a[0], a[1], a[2], a[3], a[4], a[5], a[6])
    print(UNREACHABLE if result == UNREACHABLE else json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1:])
