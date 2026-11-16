"""One request to the Cognee server, for the plugin's shell scripts.

The shell scripts (``cognee-forget.sh``, ``cognee-index-repo.sh``) never hold
the key: they name the request and this helper sends it, with the server
address and key resolved the way the hooks resolve them.

    _authed_request.py url              print the server address
    _authed_request.py has-key          exit 0 when a key is available, 3 when the
                                        key is not for this server, else 2
    _authed_request.py GET <url>        send the request, print the answer
    _authed_request.py POST <url>       the JSON body is read from stdin

The answer is printed as the body, then a line ``HTTP <status>``, which the
callers check. A request is only sent to the configured server: any other
address is refused. When the relay runs this helper, the Cognee API key option
goes only to the server the relay resolved from the options; a caller that
points the run at another server gets no key (``has-key`` exits 3 and says so).

Stdlib only.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

# Nothing here needs the plugin's virtualenv; this keeps the import below from
# re-running the script inside it.
os.environ["COGNEE_PLUGIN_IN_VENV"] = "1"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TIMEOUT_SECONDS = 120.0


def _server() -> str:
    import _plugin_common

    return _plugin_common._normalize_service_url(_plugin_common._local_api_url())


def _server_key(server: str) -> str:
    import _plugin_common

    return _plugin_common.shell_api_key(server)


def _say_refused(server: str) -> bool:
    """Explain a key withheld because the run was aimed at another server."""
    import _plugin_common

    bound = _plugin_common.key_refused_for(server)
    if bound:
        sys.stderr.write(
            "Error: the Cognee API key is only sent to %s; this command asked for %s.\n"
            % (bound, server)
        )
    return bool(bound)


def _send(method: str, url: str, body: bytes | None) -> int:
    import _plugin_common

    server = _server()
    if not (url == server or url.startswith(server + "/")):
        sys.stderr.write("Error: %s is not on the configured server %s\n" % (url, server))
        return 1
    headers = {"X-Api-Key": _server_key(server)}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(
            request, timeout=_TIMEOUT_SECONDS, context=_plugin_common._https_context()
        ) as answer:
            status, text = answer.status, answer.read()
    except urllib.error.HTTPError as error:
        status, text = error.code, error.read()
    except (urllib.error.URLError, OSError) as error:
        sys.stderr.write("Error: could not reach %s (%s)\n" % (server, error))
        return 7
    sys.stdout.buffer.write(text)
    sys.stdout.write("\nHTTP %d\n" % status)
    return 0


def main(argv: list) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "url":
        print(_server())
        return 0
    if command == "has-key":
        server = _server()
        if _server_key(server):
            return 0
        return 3 if _say_refused(server) else 2
    if command == "GET" and len(argv) == 3:
        return _send("GET", argv[2], None)
    if command == "POST" and len(argv) == 3:
        return _send("POST", argv[2], sys.stdin.buffer.read())
    sys.stderr.write("usage: _authed_request.py url | has-key | GET <url> | POST <url>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
