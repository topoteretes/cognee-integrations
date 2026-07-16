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
import sys
import time

from _recall_http import UNREACHABLE, _error, do_recall

# Tunables (mirror Hermes's provider defaults).
_THRESHOLD = int(os.environ.get("COGNEE_BREAKER_THRESHOLD", "5"))
_COOLDOWN = float(os.environ.get("COGNEE_BREAKER_COOLDOWN", "120"))
_RECALL_TIMEOUT = float(os.environ.get("COGNEE_RECALL_TIMEOUT", "20"))


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

    result = do_recall(
        service_url,
        api_key,
        query,
        session_id,
        scope,
        top_k,
        dataset,
        timeout=timeout or _RECALL_TIMEOUT,
    )
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
