#!/usr/bin/env python3
"""Server-first remember against Cognee's ``/api/v1/remember``.

Standalone, stdlib-only, so it runs under the system ``python3`` without the
plugin venv (the same constraint ``cognee-search.sh`` already works under).

Contract — what gets printed to stdout:
  * ``{"ok": true}`` on a 2xx response.
  * the sentinel ``UNREACHABLE`` ONLY when the server cannot be reached
    (connection refused, timeout, DNS). The caller may then fall back to the
    local CLI as a degraded path.
  * a JSON **error object** ``{"error", "status", "authoritative": false}`` on
    any HTTP error (5xx, 4xx, and especially **401/403** auth rejections). The
    caller MUST NOT fall back to the local CLI here: the server was reachable and
    rejected/failed the request, so falling back could bypass auth boundaries
    or silently double-write to a different backend.

Diagnostics also go to stderr so the caller can surface them.
"""

import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

UNREACHABLE = "UNREACHABLE"

# Remember (write) timeout in seconds, tunable independently of recall
# (COGNEE_RECALL_TIMEOUT). Falls back to the prior default when unset.
_REMEMBER_TIMEOUT = float(os.environ.get("COGNEE_REMEMBER_TIMEOUT", "60"))


def _is_local(url):
    """True for a localhost/loopback target (where a cloud API key is meaningless)."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def _multipart_body(fields, files):
    boundary = f"----cogneeRemember{uuid.uuid4().hex}"
    chunks = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for field_name, filename, content in files:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(content if isinstance(content, bytes) else content.encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


def _error(status, message):
    """An error envelope — reachable server, but the request was rejected/failed.

    Distinct from UNREACHABLE so the caller does NOT fall back to the local CLI.
    """
    return {"error": message, "status": status, "authoritative": False}


def _background_flag():
    """Whether the server should cognify in the background (default: yes).

    A synchronous cognify (run_in_background=false) can take tens of seconds: it
    blocks the agent's turn and risks the client read-timeout that gets misread as
    "server unreachable" (then a CLI fallback that can double-write). Background lets
    the POST return as soon as the work is enqueued. Set COGNEE_REMEMBER_BACKGROUND=false
    for a synchronous, immediately-queryable write.
    """
    val = os.environ.get("COGNEE_REMEMBER_BACKGROUND", "").strip().lower()
    return "false" if val in {"0", "false", "no", "off"} else "true"


def _timeout_result(timeout):
    """A write timeout is NOT 'unreachable': the request likely reached the server and
    the write may have landed. Returning UNREACHABLE would make the caller re-write via
    the CLI and risk a duplicate, so surface a non-fatal note instead — the caller
    prints it and does NOT fall back. Background writes make this path rare.
    """
    sys.stderr.write(
        "[cognee-remember] timed out after %ss waiting for confirmation; the write may "
        "have succeeded — NOT falling back to local CLI\n" % timeout
    )
    return _error(0, "remember submitted; timed out after %ss waiting for confirmation" % timeout)


def do_remember(
    service_url,
    api_key,
    content,
    dataset,
    node_set,
    *,
    opener=urllib.request.urlopen,
    timeout=_REMEMBER_TIMEOUT,
):
    """POST content to the server. Return {"ok": true}, an error envelope, or UNREACHABLE."""
    url = service_url.rstrip("/") + "/api/v1/remember"
    filename = f"{node_set or 'content'}.txt"
    body, boundary = _multipart_body(
        {
            "datasetName": dataset,
            "node_set": node_set,
            "run_in_background": _background_flag(),
        },
        [("data", filename, content.encode("utf-8") if isinstance(content, str) else content)],
    )
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    # COGNEE_API_KEY is a cloud credential; local single-user servers need no
    # auth and ignore it. Only attach it for a remote/cloud target.
    if api_key and not _is_local(service_url):
        headers["X-Api-Key"] = api_key

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            msg = "unauthorized (HTTP %s) — check COGNEE_API_KEY / credentials" % e.code
        else:
            msg = "server returned HTTP %s for /api/v1/remember" % e.code
        sys.stderr.write("[cognee-remember] %s — NOT falling back to local CLI\n" % msg)
        return _error(e.code, msg)
    except (TimeoutError, socket.timeout):
        return _timeout_result(timeout)
    except urllib.error.URLError as e:
        # A read timeout arrives wrapped in URLError on some platforms — treat it as
        # a timeout, not "unreachable" (the request likely reached the server).
        if isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
            return _timeout_result(timeout)
        sys.stderr.write(
            "[cognee-remember] server unreachable at %s: %s\n" % (service_url, str(e)[:160])
        )
        return UNREACHABLE
    except Exception as e:
        sys.stderr.write(
            "[cognee-remember] server unreachable at %s: %s\n" % (service_url, str(e)[:160])
        )
        return UNREACHABLE

    # The server responded with 2xx. A body we can't parse is fine —
    # the important thing is the server accepted the request.
    try:
        data = json.loads(raw or "{}")
    except (json.JSONDecodeError, ValueError):
        return {"ok": True}

    if isinstance(data, dict) and data.get("error"):
        msg = str(data.get("error"))[:200]
        sys.stderr.write("[cognee-remember] server returned error: %s\n" % msg)
        return _error(200, msg)
    return {"ok": True}


def main(argv):
    # argv: service_url, api_key, content, dataset, node_set
    a = list(argv) + [""] * 5
    result = do_remember(a[0], a[1], a[2], a[3], a[4])
    print(UNREACHABLE if result == UNREACHABLE else json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1:])
