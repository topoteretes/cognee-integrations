#!/usr/bin/env python3
"""Server access for the cognee-forget skill (``cognee-forget.sh`` runs this).

The commands are the ones ``cognee-forget.sh`` documents (``USAGE`` below):
``datasets``, ``data``, ``raw``, ``forget``, ``forget-memory``,
``forget-dataset`` and ``forget-everything --confirm-delete-all``.

Every API command prints the response body followed by a final
``HTTP <status>`` line, and exits 0 on an HTTP error, deliberately, so a 404
"already deleted" stays inspectable: callers must check that line.

The request is made here, in one process. The server's address comes from the
plugin's settings and the request is authorized the way every plugin script
is (``_plugin_common``): against the plugin's local server, as that server's
owner. Nothing is printed, exported or handed to another process. In Cloud
mode a command run from a shell cannot be authorized, and this exits 2 before
any request.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_COMMANDS = (
    ("sync", "Flush the live session into documents (run before listing)"),
    ("datasets", "List datasets (find the plugin dataset's id)"),
    ("data <dataset_id>", "List the data items in a dataset"),
    ("raw <dataset_id> <data_id>", "Print a data item's raw stored content"),
    ("forget <dataset_id> <data_id>", "Delete one data item (IRREVERSIBLE)"),
    (
        "forget-memory <dataset_id>",
        "Clear a dataset's graph and vectors, keep raw data (IRREVERSIBLE)",
    ),
    ("forget-dataset <dataset_id>", "Delete a whole dataset (IRREVERSIBLE)"),
    ("forget-everything --confirm-delete-all", "Delete ALL of the user's data (IRREVERSIBLE)"),
)
USAGE = "Usage:\n" + "".join(
    f"  cognee-forget.sh {command:<31} {what}\n"
    if len(command) <= 31
    else f"  cognee-forget.sh {command}\n{' ' * 51}{what}\n"
    for command, what in _COMMANDS
)

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# A whole-dataset or delete-all request can run long on a large store.
_TIMEOUT = 600.0


def _fail(message: str, *, usage: bool = False, code: int = 1) -> None:
    print(message, file=sys.stderr)
    if usage:
        sys.stderr.write(USAGE)
    raise SystemExit(code)


def _uuid(value: str) -> str:
    """Ids reach both the request URL and the JSON body, so anything that is
    not a canonical UUID is refused before it is used: a crafted value could
    otherwise redirect the request to a different endpoint."""
    if not _UUID.fullmatch(value):
        _fail(f"Error: invalid id (expected a UUID): {value}")
    return value


def _service_url() -> str:
    """The server from the plugin's settings, or "" when they cannot be read.

    Never the shell's own ``COGNEE_BASE_URL``: without the loader the
    environment is still the one the shell exported, which is not a plugin
    setting, so nothing is resolved from it.
    """
    try:
        from _env_file import load_env_file

        load_env_file()
    except Exception:
        return ""
    return (
        (
            os.environ.get("COGNEE_LOCAL_API_URL")
            or os.environ.get("COGNEE_BASE_URL")
            or "http://localhost:8011"
        )
        .strip()
        .rstrip("/")
    )


def _authorization(service_url: str) -> str:
    """What authorizes a request to ``service_url``, or "" when nothing does."""
    # The opt-out keeps _plugin_common from re-execing this command into the
    # plugin venv: nothing here needs it.
    os.environ["COGNEE_PLUGIN_IN_VENV"] = "1"
    try:
        from _plugin_common import _api_key_with_source

        return _api_key_with_source(service_url)[0]
    except Exception:
        return ""


def _send(service_url: str, auth: str, method: str, path: str, payload=None) -> int:
    """Send one request and print the body and the ``HTTP <status>`` line."""
    from _net import open_url

    headers = {"X-Api-Key": auth}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(service_url + path, data=data, headers=headers, method=method)
    context = None
    if service_url.lower().startswith("https:"):
        try:
            from _plugin_common import _https_context

            context = _https_context()
        except Exception:
            context = None
    try:
        with open_url(req, timeout=_TIMEOUT, context=context) as resp:
            status, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"Error: could not reach the Cognee server at {service_url}: {exc}", file=sys.stderr)
        print("\nHTTP 000")
        return 1
    out = sys.stdout.buffer
    out.write(body)
    out.write(("\nHTTP %d\n" % status).encode("ascii"))
    out.flush()
    return 0


def main(argv: list[str]) -> int:
    service_url = _service_url()
    if not service_url:
        _fail(
            "Error: could not resolve the Cognee server URL from the plugin's settings "
            "(is python3 available?)."
        )
    auth = _authorization(service_url)
    if not auth:
        print("Error: no API key resolved.", file=sys.stderr)
        _fail(
            "Cloud mode: the Cognee Cloud key is a plugin option that Claude Code passes to "
            "the plugin's hooks only, so this command cannot use it. Local mode: the key "
            "comes from the local server, which may not be running — start a new session or "
            "run cognee-doctor.sh.",
            code=2,
        )

    cmd = argv[0] if argv else ""
    first = argv[1] if len(argv) > 1 else ""
    second = argv[2] if len(argv) > 2 else ""

    if cmd == "datasets":
        return _send(service_url, auth, "GET", "/api/v1/datasets")
    if cmd == "data":
        if not first:
            _fail("Error: data requires <dataset_id>", usage=True)
        return _send(service_url, auth, "GET", f"/api/v1/datasets/{_uuid(first)}/data")
    if cmd == "raw":
        if not (first and second):
            _fail("Error: raw requires <dataset_id> <data_id>", usage=True)
        path = f"/api/v1/datasets/{_uuid(first)}/data/{_uuid(second)}/raw"
        return _send(service_url, auth, "GET", path)
    if cmd == "forget":
        if not (first and second):
            _fail("Error: forget requires <dataset_id> <data_id>", usage=True)
        # This command must only ever be able to express a single-document
        # delete: two validated ids, and a body with exactly these two fields.
        payload = {"datasetId": _uuid(first), "dataId": _uuid(second)}
        return _send(service_url, auth, "POST", "/api/v1/forget", payload)
    if cmd in ("forget-memory", "forget-dataset"):
        # The broader deletions, each its own command taking exactly one
        # validated dataset id. Anything after the id is refused: a second id
        # is a single-document delete typed under the wrong command, and
        # ignoring it would delete the whole dataset.
        if len(argv) != 2:
            _fail(f"Error: {cmd} takes exactly one <dataset_id>", usage=True)
        payload = {"datasetId": _uuid(first)}
        if cmd == "forget-memory":
            payload["memoryOnly"] = True
        return _send(service_url, auth, "POST", "/api/v1/forget", payload)
    if cmd == "forget-everything":
        # It names no target, so it runs only with this one literal argument
        # and nothing else: a bare command, a typo or an extra argument sends
        # no request.
        if len(argv) != 2 or first != "--confirm-delete-all":
            _fail(
                "Error: forget-everything deletes ALL of the user's data and runs only as: "
                "cognee-forget.sh forget-everything --confirm-delete-all",
                usage=True,
            )
        return _send(service_url, auth, "POST", "/api/v1/forget", {"everything": True})
    _fail(f"Error: unknown command '{cmd}'", usage=True)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
