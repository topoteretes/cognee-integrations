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

from _env_file import load_env_file
from _recall_http import UNREACHABLE, _error, do_recall

# ~/.cognee/.env must land in os.environ before the module-level reads below.
load_env_file()

# Tunables (mirror Hermes's provider defaults).
_THRESHOLD = int(os.environ.get("COGNEE_BREAKER_THRESHOLD", "5"))
_COOLDOWN = float(os.environ.get("COGNEE_BREAKER_COOLDOWN", "120"))
# Sliding window: only failures within this many seconds of each other count
# toward the threshold. Without it, five isolated blips spread over days would
# open the breaker "out of nowhere" long after the last real problem.
_WINDOW = float(os.environ.get("COGNEE_BREAKER_WINDOW", "300"))
_RECALL_TIMEOUT = float(os.environ.get("COGNEE_RECALL_TIMEOUT", "120"))


def _state_path():
    base = os.environ.get("COGNEE_PLUGIN_STATE_DIR") or os.path.expanduser("~/.cognee-plugin")
    return pathlib.Path(base) / "recall-breaker.json"


def _norm_url(service_url):
    return str(service_url or "").strip().rstrip("/")


def _read():
    """The per-server breaker map {url: entry}. A legacy flat file (top-level
    ``failures``/``cooldown_until``, not keyed by server) is discarded rather
    than migrated: it conflated every server on the machine, which is one of
    the bugs this schema exists to fix."""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    servers = data.get("servers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def _write(servers):
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # pid-suffixed tmp + atomic replace, matching the other marker writers:
        # readers (other hooks, the status-line renderer) can never see a torn
        # file. Concurrent writers remain last-write-wins — accepted for this
        # advisory state; serializing them would need file locking.
        tmp = path.with_name(".breaker-%d.json.tmp" % os.getpid())
        tmp.write_text(json.dumps({"servers": servers}), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def _entry(servers, url):
    entry = servers.get(url)
    return entry if isinstance(entry, dict) else {}


def _recent_failures(entry, now):
    """This server's failure timestamps still inside the sliding window."""
    stamps = entry.get("failures")
    if not isinstance(stamps, list):
        return []
    out = []
    for value in stamps:
        try:
            ts = float(value)
        except (TypeError, ValueError):
            continue
        if now - ts <= _WINDOW:
            out.append(ts)
    return out


def breaker_open(service_url="", now=None):
    """Return (is_open, retry_in_seconds) for this server.

    Open while inside the cooldown window. With no ``service_url`` (doctor /
    legacy callers), reports the worst open entry across all servers.
    """
    now = time.time() if now is None else now
    servers = _read()
    urls = [_norm_url(service_url)] if _norm_url(service_url) else list(servers)
    worst = 0.0
    for url in urls:
        try:
            until = float(_entry(servers, url).get("cooldown_until") or 0.0)
        except (TypeError, ValueError):
            until = 0.0
        worst = max(worst, until)
    return (True, int(worst - now)) if now < worst else (False, 0)


def breaker_reason(service_url=""):
    """The reason the breaker last tripped for this server ("" if never)."""
    servers = _read()
    url = _norm_url(service_url)
    if not url and servers:
        url = next(iter(servers))
    return str(_entry(servers, url).get("reason") or "")


def record_failure(error="", now=None, service_url="", reason="unreachable"):
    """Count a hard backend failure; open the breaker at the windowed threshold.

    Only definitive trouble belongs here — UNREACHABLE (positively absent) or
    5xx. Timeouts are no-verdict and must NOT be recorded (see the transient
    envelope handling in ``recall``). When the breaker trips, the counted
    failures are consumed: after the cooldown the breaker is half-open, and a
    single new failure starts a fresh count instead of instantly re-opening.
    """
    now = time.time() if now is None else now
    url = _norm_url(service_url)
    servers = _read()
    entry = _entry(servers, url)
    failures = _recent_failures(entry, now)
    failures.append(now)
    if len(failures) >= _THRESHOLD:
        entry = {
            "failures": [],
            "cooldown_until": now + _COOLDOWN,
            "reason": str(reason or "unreachable"),
            "last_error": str(error)[:200],
        }
    else:
        entry = dict(entry)
        entry["failures"] = failures
        entry["last_error"] = str(error)[:200]
    servers[url] = entry
    _write(servers)


def record_success(service_url=""):
    """Backend answered — clear this server's breaker entry."""
    servers = _read()
    servers.pop(_norm_url(service_url), None)
    _write(servers)


def recall(service_url, api_key, query, session_id, scope, top_k, dataset="", *, timeout=None):
    """Breaker-wrapped recall. Returns a list, an error-envelope dict, or UNREACHABLE.

    Only genuine backend trouble trips the breaker: UNREACHABLE (positively
    absent — refused/DNS) or a 5xx. A reachable 4xx (e.g. 401/403 auth) is a
    config problem — surfaced, but it does NOT open the breaker (waiting
    wouldn't fix it). A transient envelope (timeout / unclassifiable transport
    error) is no verdict at all: it neither counts as a failure nor clears the
    window — a busy server must not be branded unreachable by its own latency.
    """
    is_open, retry = breaker_open(service_url)
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
        record_failure("unreachable", service_url=service_url, reason="unreachable")
    elif isinstance(result, dict) and int(result.get("status") or 0) >= 500:
        record_failure(
            "http %s" % result.get("status"), service_url=service_url, reason="server_error"
        )
    elif isinstance(result, dict) and result.get("transient"):
        pass  # no verdict: leave the breaker exactly as it was
    else:
        record_success(service_url)
    return result


def main(argv):
    # argv: service_url, api_key, query, session_id, scope, top_k[, dataset]
    a = list(argv) + [""] * 7
    result = recall(a[0], a[1], a[2], a[3], a[4], a[5], a[6])
    print(UNREACHABLE if result == UNREACHABLE else json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1:])
