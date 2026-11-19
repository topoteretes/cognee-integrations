#!/usr/bin/env python3
"""Server-first recall against Cognee's ``/api/v1/recall``.

Standalone, stdlib-only, so it runs under the system ``python3`` without the
plugin venv (the same constraint ``cognee-search.sh`` already works under).

Contract — what gets printed to stdout:
  * a JSON **list** on a 2xx response. An **empty list is authoritative**:
    the server searched and found nothing.
  * the sentinel ``UNREACHABLE`` ONLY when the server is positively absent
    (connection refused, DNS failure, unroutable host). The caller may then
    fall back to the local CLI as a degraded path. A **timeout is NOT
    unreachable**: a dead server refuses in milliseconds, a busy one times
    out — so timeouts return a *transient* error envelope instead (see below),
    and the caller keeps its prior view of the server rather than declaring
    it down.
  * a JSON **error object** ``{"error", "status", "authoritative": false}`` on
    any HTTP error (5xx, 4xx, and especially **401/403** auth rejections) or an
    error-shaped 2xx body. The caller MUST NOT fall back to the local CLI here:
    the server was reachable and rejected/failed the request, so falling back to
    a (possibly different / local) backend would return wrong data or bypass the
    server-side authorization boundary. It is reported as an error, never as
    "no results".

Diagnostics also go to stderr so the caller can surface them.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

UNREACHABLE = "UNREACHABLE"

# Transport-exception verdicts (classify_transport_exception). Only DOWN is
# evidence the server is absent; SLOW means it exists but did not answer in
# time, and UNKNOWN is anything we cannot classify. The distinction matters:
# a dead local server refuses connections in milliseconds, while a busy one
# times out — conflating the two is what painted false "unreachable" states.
DOWN = "down"
SLOW = "slow"
UNKNOWN = "unknown"

# Errnos that positively identify an absent/unroutable server.
_DOWN_ERRNOS = {errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH}


def classify_transport_exception(exc) -> str:
    """Classify a transport failure as DOWN, SLOW, or UNKNOWN.

    Unwraps ``urllib.error.URLError`` (the real cause lives in ``.reason``, and
    can be an exception *or* a plain string). Order matters below:
    ``TimeoutError`` and ``ConnectionRefusedError`` are OSError subclasses, and
    ``ssl.SSLError`` is too, so the generic errno check must come last.
    """
    if isinstance(exc, urllib.error.HTTPError):
        # The server answered; HTTP statuses are the caller's business.
        return UNKNOWN
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason
    if isinstance(exc, str):
        return SLOW if "timed out" in exc else UNKNOWN
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return SLOW
    if isinstance(exc, socket.gaierror):
        return DOWN
    if isinstance(exc, ConnectionRefusedError):
        return DOWN
    if isinstance(exc, ssl.SSLError):
        return UNKNOWN
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in _DOWN_ERRNOS:
        return DOWN
    return UNKNOWN


# macOS Python installations often lack root CA certs in the default bundle.
# Build one opener for all HTTPS calls: try certifi opportunistically (if
# importable), then walk system cert file locations until one loads cleanly.
def _build_https_opener():
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
        _cert_loaded = False
        for path in filter(
            None,
            [
                os.environ.get("SSL_CERT_FILE"),
                "/etc/ssl/cert.pem",
                "/etc/ssl/certs/ca-certificates.crt",
            ],
        ):
            if os.path.exists(path):
                try:
                    ctx.load_verify_locations(path)
                    _cert_loaded = True
                    break  # only stop once a path loaded successfully
                except Exception:
                    pass
        if not _cert_loaded:
            sys.stderr.write(
                "[cognee-search] SSL: no system cert bundle loaded; HTTPS may fail"
                " — set SSL_CERT_FILE or install certifi\n"
            )
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


_HTTPS_OPENER = _build_https_opener()


def coerce_top_k(value, default=5):
    """Best-effort positive int; never raises (a bad value must not look like a server failure)."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return n if n > 0 else default


def coerce_scope(value, default=None):
    """Parse the JSON scope arg; graph-only on anything empty or malformed.

    Memory is read from the graph and the code graph only. The server's
    ``auto`` scope would fold raw session entries in, so it is never the
    fallback here.
    """
    if default is None:
        default = ["graph"]
    if not value:
        return list(default)
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return list(default)


def _error(status, message, *, transient=False):
    """An error envelope — the request failed, but the server is NOT known dead.

    Distinct from UNREACHABLE so the caller does NOT fall back to the local CLI.
    ``transient=True`` marks a no-verdict failure (timeout / unclassifiable
    transport error): the breaker must count it as neither success nor failure,
    and no connection state should be rewritten because of it.
    """
    envelope = {"error": message, "status": status, "authoritative": False}
    if transient:
        envelope["transient"] = True
    return envelope


def _searched_target(body):
    """What a recall body searched, for error messages: dataset name(s) or id(s)."""
    names = body.get("datasets") or ([body["dataset"]] if body.get("dataset") else [])
    if names:
        return "dataset " + ", ".join(str(n) for n in names)
    ids = body.get("dataset_ids") or []
    if ids:
        return "dataset id " + ", ".join(str(i) for i in ids)
    return ""


def _server_error_detail(error, limit=400):
    """The server's error message from an HTTPError body ('' when there is none)."""
    try:
        raw = error.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return ""
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = raw
    if isinstance(parsed, dict):
        for key in ("detail", "message", "error"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                parsed = value
                break
            if isinstance(value, dict) and isinstance(value.get("message"), str):
                parsed = value["message"]
                break
    text = parsed if isinstance(parsed, str) else json.dumps(parsed)
    return " ".join(text.split())[:limit]


def coerce_code_query(value):
    """Parse the JSON code_query arg; None on anything empty or malformed.

    A malformed code_query must degrade to "no code lane", never to a server
    422 that would read as a recall failure.
    """
    if not value:
        return None
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def coerce_dataset_ids(value):
    """Normalise ``dataset_ids`` from argv (comma-separated) or a list to a clean list."""
    if not value:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [str(x).strip() for x in value if str(x).strip()]


def do_recall(
    service_url,
    api_key,
    query,
    session_id,
    scope,
    top_k,
    dataset="",
    context_profile="",
    code_query=None,
    dataset_ids="",
    *,
    opener=None,
    timeout=120.0,
):
    """Query the server. Return results (list), an error envelope (dict), or ``UNREACHABLE``.

    ``dataset_ids`` (a list, or a comma-separated string from argv) addresses
    the search by UUID and takes precedence over ``dataset`` — under shared
    agent memory the launch's dataset is a canonical parent-owned one the
    agent can only reach by id, since a name resolves among owned datasets.
    """
    url = service_url.rstrip("/") + "/api/v1/recall"
    body = {
        "query": query,
        "top_k": coerce_top_k(top_k),
        "only_context": True,
        "scope": coerce_scope(scope),
    }
    # Deterministic code-graph lane (cognee >= 1.5.3): only meaningful when
    # the scope includes "code" — the server rejects code_query without it.
    parsed_code_query = coerce_code_query(code_query)
    if parsed_code_query is not None:
        body["code_query"] = parsed_code_query
    if session_id:
        body["session_id"] = session_id
    # Scope the search to the caller's plugin dataset (resolved by the shell from
    # COGNEE_PLUGIN_DATASET → default). All plugin writes target
    # that single dataset, so searching elsewhere only adds noise from unrelated
    # sessions or SDK calls (e.g. client.py defaulting to 'default_dataset').
    # Server-side RBAC is still enforced: the named dataset must be owned by the
    # authenticated user or the server returns DatasetNotFoundError.
    # When dataset is empty (standalone invocation without shell), fall back to
    # the original search-all behaviour to avoid breaking direct callers.
    from _dataset_access import recall_fields

    # Precedence: COGNEE_PLUGIN_READ_DATASET_IDS on a graph-only recall (the
    # user's own federated read set; session history stays bound to ONE
    # dataset, so the session id is dropped), then the UUIDs shared memory
    # resolved for the launch, then the dataset itself (id when UUID-shaped).
    fields, federated = recall_fields(dataset, body["scope"])
    ids = coerce_dataset_ids(dataset_ids)
    if ids and body["scope"] != ["graph"]:
        # Session history is bound to ONE dataset — the canonical write dataset,
        # first in the resolved list; same-named copies only widen graph recall.
        ids = ids[:1]
    if federated:
        body.update(fields)
        body.pop("session_id", None)
    elif ids:
        body["dataset_ids"] = ids
    else:
        body.update(fields)
    if context_profile:
        body["context_profile"] = context_profile
    headers = {"Content-Type": "application/json"}
    # Always attach the key when present: cognee >=1.2.2 enforces auth on its
    # API routes even on localhost (the local key is auto-minted at bootstrap),
    # and a server running with auth disabled simply ignores the header.
    if api_key:
        headers["X-Api-Key"] = api_key

    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    _open = opener if opener is not None else _HTTPS_OPENER.open
    try:
        with _open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # Reachable but rejected/failed. NOT an authoritative empty, and NOT a
        # reason to query a different backend via the CLI — report the error.
        if e.code == 404:
            # cognee >= 1.6.0 answers a dataset with no graph yet, or a dataset
            # name that resolves to nothing, with 404 (DatasetNotFoundError)
            # instead of an empty list. Nothing can be found there: an
            # authoritative empty, not a failure, and not a reason to fall back.
            sys.stderr.write(
                "[cognee-search] no graph for this dataset yet (HTTP 404) — empty result\n"
            )
            return []
        if e.code in (401, 403):
            msg = "unauthorized (HTTP %s) — check COGNEE_API_KEY / credentials" % e.code
        else:
            msg = "server returned HTTP %s for /api/v1/recall" % e.code
            # Name what was searched and pass the server's own reason on: the
            # server's message identifies a dataset only by UUID, and a bare
            # status code leaves a model reading this to guess the rest.
            target = _searched_target(body)
            if target:
                msg += " (searched %s)" % target
            detail = _server_error_detail(e)
            if detail:
                msg += ": " + detail
        sys.stderr.write("[cognee-search] %s — NOT falling back to local CLI\n" % msg)
        return _error(e.code, msg)
    except Exception as e:
        verdict = classify_transport_exception(e)
        if verdict == DOWN:  # refused / DNS / unroutable → positively absent
            sys.stderr.write(
                "[cognee-search] server unreachable at %s: %s\n" % (service_url, str(e)[:160])
            )
            return UNREACHABLE
        # SLOW (timed out — alive but busy) or UNKNOWN (SSL / reset / a bug in
        # our own request building): no verdict on the server. Not UNREACHABLE
        # (no CLI fallback, no "down" marker) and flagged transient so the
        # breaker counts it as neither success nor failure.
        sys.stderr.write(
            "[cognee-search] no verdict (%s) from %s: %s\n" % (verdict, service_url, str(e)[:160])
        )
        return _error(0, "recall %s: %s" % (verdict, str(e)[:160]), transient=True)

    # The server responded. A body we can't parse is a SERVER-side bug, not an
    # unreachable server — report it as an error (do NOT trigger the CLI fallback).
    try:
        data = json.loads(raw or "[]")
    except (json.JSONDecodeError, ValueError) as e:
        sys.stderr.write("[cognee-search] malformed JSON from /api/v1/recall: %s\n" % str(e)[:160])
        return _error(200, "malformed JSON response from /api/v1/recall")

    # An error-shaped 2xx body is also not a real result set.
    if isinstance(data, dict) and data.get("error"):
        msg = str(data.get("error"))[:200]
        sys.stderr.write("[cognee-search] server returned error: %s\n" % msg)
        return _error(200, msg)
    if isinstance(data, list):
        return data
    return [data]


def main(argv):
    # argv: service_url, api_key, query, session_id, scope, top_k[, dataset
    #        [, context_profile[, code_query[, dataset_ids]]]]
    # code_query (arg 9): JSON dict for the deterministic "code" scope, e.g.
    # '{"operation": "impact_analysis", "targets": ["process_payment"]}'.
    # dataset_ids (arg 10): comma-separated UUIDs; wins over the dataset name.
    a = list(argv) + [""] * 10
    result = do_recall(a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7], a[8], a[9])
    # UNREACHABLE → caller falls back to CLI; a list (results) or an error
    # object → caller prints as-is and does NOT fall back.
    print(UNREACHABLE if result == UNREACHABLE else json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1:])
