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


def annotate_empty_recall(result, session_id):
    """Distinguish "nothing stored" from "captured, not yet cognified" on empty recall.

    A 2xx empty list stays authoritative, but when the local session-cache bridge
    still holds qa/trace content whose digest the drain has not marked cognified,
    a bare ``[]`` would read as "no memory" and push the caller to transcript
    grep. Return a distinct envelope carrying the pending counts instead. Every
    non-empty result (hits, error envelope, UNREACHABLE) passes through unchanged,
    as does a genuine empty (no pending captures).
    """
    if result != []:
        return result
    try:
        # Lazy import: _plugin_common is stdlib-only, but keep this script
        # functional standalone even if the module is absent/unreadable.
        from _plugin_common import pending_capture_counts

        pending = pending_capture_counts(session_id)
    except Exception:
        return result
    if not any(pending.values()):
        return result
    return {
        "recall": [],
        "captured_pending": pending,
        "authoritative": True,
        "hint": (
            "content captured this session is not yet cognified; "
            "retry shortly (the background sync picks it up) or run "
            'python3 "${CODEX_PLUGIN_ROOT}/scripts/sync-session-to-graph.py"; '
            "do not fall back to transcript search"
        ),
    }


def main(argv):
    # argv: service_url, api_key, query, session_id, scope, top_k[, dataset]
    a = list(argv) + [""] * 7
    result = annotate_empty_recall(recall(a[0], a[1], a[2], a[3], a[4], a[5], a[6]), a[3])
    print(UNREACHABLE if result == UNREACHABLE else json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1:])
