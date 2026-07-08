#!/usr/bin/env python3
"""Server-first recall against Cognee's ``/api/v1/recall``.

Standalone, stdlib-only, so it runs under the system ``python3`` without the
plugin venv (the same constraint ``cognee-search.sh`` already works under).

Contract — what gets printed to stdout:
  * a JSON **list** on a 2xx response. An **empty list is authoritative**:
    the server searched and found nothing.
  * the sentinel ``UNREACHABLE`` ONLY when the server cannot be reached
    (connection refused, timeout, DNS). The caller may then fall back to the
    local CLI as a degraded path.
  * a JSON **error object** ``{"error", "status", "authoritative": false}`` on
    any HTTP error (5xx, 4xx, and especially **401/403** auth rejections) or an
    error-shaped 2xx body. The caller MUST NOT fall back to the local CLI here:
    the server was reachable and rejected/failed the request, so falling back to
    a (possibly different / local) backend would return wrong data or bypass the
    server-side authorization boundary. It is reported as an error, never as
    "no results".

Diagnostics also go to stderr so the caller can surface them.
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

UNREACHABLE = "UNREACHABLE"


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


def _is_local(url):
    """True for a localhost/loopback target (where a cloud API key is meaningless)."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def coerce_top_k(value, default=5):
    """Best-effort positive int; never raises (a bad value must not look like a server failure)."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return n if n > 0 else default


def coerce_scope(value, default="auto"):
    """Parse the JSON scope arg; fall back to "auto" on anything malformed."""
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _error(status, message):
    """An error envelope — reachable server, but the request was rejected/failed.

    Distinct from UNREACHABLE so the caller does NOT fall back to the local CLI.
    """
    return {"error": message, "status": status, "authoritative": False}


def do_recall(
    service_url,
    api_key,
    query,
    session_id,
    scope,
    top_k,
    dataset="",
    context_profile="",
    *,
    opener=None,
    timeout=20.0,
):
    """Query the server. Return results (list), an error envelope (dict), or ``UNREACHABLE``."""
    url = service_url.rstrip("/") + "/api/v1/recall"
    body = {
        "query": query,
        "top_k": coerce_top_k(top_k),
        "only_context": True,
        "scope": coerce_scope(scope),
    }
    if session_id:
        body["session_id"] = session_id
    # Scope the search to the caller's plugin dataset (resolved by the shell from
    # connections/me → COGNEE_PLUGIN_DATASET → default). All plugin writes target
    # that single dataset, so searching elsewhere only adds noise from unrelated
    # sessions or SDK calls (e.g. client.py defaulting to 'default_dataset').
    # Server-side RBAC is still enforced: the named dataset must be owned by the
    # authenticated user or the server returns DatasetNotFoundError.
    # When dataset is empty (standalone invocation without shell), fall back to
    # the original search-all behaviour to avoid breaking direct callers.
    if dataset:
        companion_enabled = str(os.environ.get("COGNEE_SESSION_COMPANION_DATASET", "")).lower() in ("true", "1", "yes")
        if companion_enabled and dataset != "agent_sessions":
            body["datasets"] = [dataset, f"{dataset}-agent_sessions"]
        else:
            body["datasets"] = [dataset]
    if context_profile:
        body["context_profile"] = context_profile
    headers = {"Content-Type": "application/json"}
    # COGNEE_API_KEY is a *cloud* credential; the local single-user server needs no
    # auth and ignores it. Only attach it for a remote/cloud target, so we don't send
    # a meaningless (and confusing) cloud key to localhost.
    if api_key and not _is_local(service_url):
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
        if e.code in (401, 403):
            msg = "unauthorized (HTTP %s) — check COGNEE_API_KEY / credentials" % e.code
        else:
            msg = "server returned HTTP %s for /api/v1/recall" % e.code
        sys.stderr.write("[cognee-search] %s — NOT falling back to local CLI\n" % msg)
        return _error(e.code, msg)
    except Exception as e:  # URLError / timeout / OSError → genuinely unreachable
        sys.stderr.write(
            "[cognee-search] server unreachable at %s: %s\n" % (service_url, str(e)[:160])
        )
        return UNREACHABLE

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
    # argv: service_url, api_key, query, session_id, scope, top_k[, dataset[, context_profile]]
    a = list(argv) + [""] * 8
    result = do_recall(a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7])
    # UNREACHABLE → caller falls back to CLI; a list (results) or an error
    # object → caller prints as-is and does NOT fall back.
    print(UNREACHABLE if result == UNREACHABLE else json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1:])
