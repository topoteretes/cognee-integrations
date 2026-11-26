#!/usr/bin/env python3
"""Client for the server's code analysis API (enola, run by the Cognee server).

The plugin never runs enola. Everything here is an HTTP call to the server the
session is connected to:

* ``analyze`` posts to ``/api/v1/code/analyze`` (``check``, ``constraints_mine``,
  ``constraints_lint``, ...). The server runs enola on the directory its
  ``code_repo`` row points at, so it needs a repository indexed by local path
  that the server can read.
* ``code_operation`` runs one deterministic CODE operation (``review``,
  ``locate``) through ``/api/v1/recall``, as ``cognee-search.sh --code`` does.

A server without the feature gets one message, never a traceback and never a
silent no-op: ``supports_analyze`` reads ``/openapi.json`` for the route (a
definite answer is cached an hour per server), and an operation the server does
not know answers 422 ``unknown_operation``, cached the same way.

Stdlib only. Every function returns a value; none raises on a server or
transport failure. ``opener`` is injectable for tests, as in ``_code_graph``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import _code_graph
from _recall_http import _HTTPS_OPENER, DOWN, _server_error_detail, classify_transport_exception

#: What every "this server cannot do it" message opens with.
REQUIREMENT = "needs a local Cognee server 1.6.11+ with a path-indexed repository"
ANALYZE_ROUTE = "/api/v1/code/analyze"
CAPABILITY_TTL_SECONDS = 3600.0

_STATE_DIR = Path.home() / ".cognee-plugin" / _code_graph._INTEGRATION / "code-review"


# -- State -------------------------------------------------------------------------


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict) -> None:
    """Atomic write; a state file that cannot be written is never fatal."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def repo_state_path(repo_root: str, prefix: str = "") -> Path:
    """This repository's file under the code-review state dir."""
    return _STATE_DIR / (prefix + _code_graph._repo_slug(repo_root) + ".json")


def _capabilities_path() -> Path:
    return _STATE_DIR / "server-capabilities.json"


def _cached_capability(service_url: str, name: str):
    entry = read_json(_capabilities_path()).get(service_url.rstrip("/"))
    entry = entry.get(name) if isinstance(entry, dict) else None
    if not isinstance(entry, dict):
        return None
    if time.time() - float(entry.get("at") or 0) >= CAPABILITY_TTL_SECONDS:
        return None
    return entry.get("ok")


def _store_capability(service_url: str, name: str, ok: bool) -> None:
    cache = read_json(_capabilities_path())
    entry = cache.get(service_url.rstrip("/"))
    entry = entry if isinstance(entry, dict) else {}
    entry[name] = {"ok": bool(ok), "at": time.time()}
    cache[service_url.rstrip("/")] = entry
    write_json(_capabilities_path(), cache)


# -- Runtime ---------------------------------------------------------------------


def is_local_url(service_url: str) -> bool:
    import urllib.parse

    url = service_url if "://" in service_url else "http://" + service_url
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def runtime() -> dict:
    """The server URL and key the skills' scripts use (as ``cognee_search``)."""
    from _env_file import load_env_file

    load_env_file()
    service_url = (
        os.environ.get("COGNEE_BASE_URL")
        or os.environ.get("COGNEE_LOCAL_API_URL")
        or "http://localhost:8011"
    ).strip()
    api_key = (os.environ.get("COGNEE_API_KEY") or "").strip()
    session_id, dataset_id, host_key = "", "", ""
    # The opt-out keeps _plugin_common from re-execing a script into the venv.
    previous = os.environ.get("COGNEE_PLUGIN_IN_VENV")
    os.environ["COGNEE_PLUGIN_IN_VENV"] = "1"
    try:
        import _plugin_common

        overrides = _plugin_common.shell_runtime_overrides(service_url)
        api_key = overrides["api_key"] or api_key
        session_id = overrides["session_id"]
        dataset_id = overrides["dataset_id"]
        host_key = overrides["host_key"]
    except Exception:
        pass
    finally:
        if previous is None:
            os.environ.pop("COGNEE_PLUGIN_IN_VENV", None)
        else:
            os.environ["COGNEE_PLUGIN_IN_VENV"] = previous
    if not api_key:
        from cognee_search import _cached_api_key

        api_key = _cached_api_key(service_url)
    return {
        "service_url": service_url,
        "api_key": api_key,
        "session_id": session_id,
        "dataset_id": dataset_id,
        "host_key": host_key,
    }


# -- Transport -------------------------------------------------------------------


def request(rt: dict, method: str, path: str, body=None, *, timeout=30.0, opener=None) -> tuple:
    """``(status, payload)``. Status 0 is a transport failure: the payload then
    has ``kind`` (``down`` / ``slow`` / ``unknown``) and ``error``. An HTTP error
    carries the server's JSON body when it has one, else ``{"detail": text}``."""
    headers = {"Content-Type": "application/json"}
    if rt.get("api_key"):
        headers["X-Api-Key"] = rt["api_key"]
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        rt["service_url"].rstrip("/") + path, data=data, headers=headers, method=method
    )
    _open = opener if opener is not None else _HTTPS_OPENER.open
    try:
        with _open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as error:
        try:
            raw = error.read().decode("utf-8", errors="replace")
            payload = json.loads(raw)
        except Exception:
            payload = {"detail": _server_error_detail(error)}
        return error.code, payload if isinstance(payload, dict) else {"detail": str(payload)}
    except Exception as error:
        return 0, {"kind": classify_transport_exception(error), "error": str(error)[:160]}
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, {"detail": "malformed JSON from " + path}


def _detail(payload) -> str:
    detail = payload.get("detail") if isinstance(payload, dict) else payload
    text = detail if isinstance(detail, str) else json.dumps(detail)
    return " ".join(str(text or "").split())


def _transport_message(rt: dict, payload: dict) -> str:
    if payload.get("kind") == DOWN:
        return "the Cognee server at %s is unreachable" % rt["service_url"]
    return "the Cognee server at %s did not answer (%s)" % (
        rt["service_url"],
        payload.get("error") or payload.get("kind"),
    )


# -- Capability ------------------------------------------------------------------


def supports_analyze(rt: dict, *, timeout=5.0, opener=None):
    """True / False when the server's API description says so, None when it could
    not be asked (never cached, so an outage does not replace a verdict)."""
    cached = _cached_capability(rt["service_url"], "analyze")
    if cached is not None:
        return bool(cached)
    status, spec = request(rt, "GET", "/openapi.json", timeout=timeout, opener=opener)
    paths = spec.get("paths") if isinstance(spec, dict) else None
    if status != 200 or not isinstance(paths, dict):
        return None
    ok = ANALYZE_ROUTE in paths
    _store_capability(rt["service_url"], "analyze", ok)
    return ok


def unsupported_message(feature: str, rt: dict, reason: str) -> str:
    where = "" if is_local_url(rt["service_url"]) else " (a remote server)"
    return "Cognee %s %s: %s. Connected to %s%s." % (
        feature,
        REQUIREMENT,
        reason,
        rt["service_url"],
        where,
    )


def indexed_repo(cwd: str, feature: str, rt: dict) -> tuple:
    """``(state, "")`` for a checkout indexed by path, else ``({}, message)``."""
    state = _code_graph.find_indexed_repo(cwd)
    if state.get("spec_kind") == "path" and state.get("dataset") and state.get("repo_root"):
        return state, ""
    urls = [
        s
        for s in _code_graph.load_repo_states()
        if s.get("spec_kind") == "url"
        and _code_graph._readable_tail(str(s.get("spec") or ""))
        == os.path.basename(_code_graph.git_repo_root(cwd) or os.path.realpath(cwd))
    ]
    if urls:
        reason = (
            "this repository was indexed from a git URL, so the server holds a shallow "
            "clone of pushed commits and no working tree"
        )
    else:
        reason = (
            "this checkout is not indexed by path (index it with "
            "cognee-index-repo.sh <repo-path> against a server on this machine)"
        )
    return {}, unsupported_message(feature, rt, reason)


# -- Calls -----------------------------------------------------------------------

#: 422 codes that mean "this server or repository cannot run the analysis".
_REPOSITORY_CODES = {
    "shallow_clone": "the server's copy is a shallow clone, which has no git history to analyse",
    "repository_unavailable": "the server cannot find the repository's directory any more",
    "no_code_repository": "the dataset holds no code repository",
    "not_indexed": "the repository's code graph is not loaded on the server yet",
    "multi_repository_dataset": (
        "the dataset holds several repositories and this analysis runs on one"
    ),
}


def analyze(
    rt: dict, feature: str, analysis: str, params: dict, dataset: str, *, timeout=600.0, opener=None
) -> dict:
    """``{"ok": True, "result": <server reply>}`` or ``{"ok": False, "message", "code"}``."""
    supported = supports_analyze(rt, opener=opener)
    if supported is False:
        return _failure(
            "unsupported",
            unsupported_message(feature, rt, "this server has no " + ANALYZE_ROUTE + " route"),
        )
    body = {"analysis": analysis, "params": params, "dataset": dataset}
    status, payload = request(rt, "POST", ANALYZE_ROUTE, body, timeout=timeout, opener=opener)
    if status == 200 and isinstance(payload, dict) and "exit_code" in payload:
        return {"ok": True, "result": payload}
    if status == 0:
        return _failure(
            "unreachable", "Cognee %s: %s." % (feature, _transport_message(rt, payload))
        )
    detail = _detail(payload)
    code = str(payload.get("code") or "") if isinstance(payload, dict) else ""
    if status == 403:
        return _failure(
            "path_refused",
            unsupported_message(
                feature,
                rt,
                "the server refused the repository path (HTTP 403: %s). A server you start "
                "yourself needs ACCEPT_LOCAL_FILE_PATH=true in its environment" % detail[:300],
            ),
        )
    if code in _REPOSITORY_CODES:
        return _failure(
            code,
            unsupported_message(feature, rt, _REPOSITORY_CODES[code] + " (" + detail[:300] + ")"),
        )
    if status == 401:
        return _failure(
            "unauthorized", "Cognee %s: unauthorized (HTTP 401); check COGNEE_API_KEY." % feature
        )
    if status == 404 and supported is None and detail in ("Not Found", ""):
        return _failure(
            "unsupported",
            unsupported_message(feature, rt, "this server has no " + ANALYZE_ROUTE + " route"),
        )
    if status == 404:
        return _failure(
            "dataset_not_found",
            "Cognee %s: the code dataset %s was not found on the server, or this key has no "
            "write permission on it (HTTP 404: %s)." % (feature, dataset, detail[:300]),
        )
    return _failure(
        code or "http_%s" % status,
        "Cognee %s: the server could not run %s (HTTP %s): %s"
        % (feature, analysis, status, detail[-600:] or "no detail"),
    )


def _failure(code: str, message: str) -> dict:
    return {"ok": False, "code": code, "message": message}


def code_operation(rt: dict, dataset: str, code_query: dict, *, timeout=30.0, opener=None) -> dict:
    """One CODE operation: ``{"ok": True, "raw": <operation result>}`` or a failure.

    A server that does not know the operation fails with ``unknown_operation``,
    remembered an hour per server (``operation_known`` then answers False).
    """
    operation = str(code_query.get("operation") or "")
    body = {
        # The CODE retriever reads the query as its seed (for ``review``, the
        # repository filter), so it is the repository's name, never free text.
        "query": str(code_query.get("repo") or operation),
        "top_k": 1,
        "only_context": True,
        "scope": ["code"],
        "datasets": [dataset],
        "code_query": code_query,
    }
    status, payload = request(rt, "POST", "/api/v1/recall", body, timeout=timeout, opener=opener)
    if status == 200:
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            raw = row.get("raw") if isinstance(row, dict) else None
            if not isinstance(raw, dict) and isinstance(row, dict):
                try:
                    raw = json.loads(str(row.get("text") or ""))
                except ValueError:
                    raw = None
            if isinstance(raw, dict) and raw.get("operation") == operation:
                return {"ok": True, "raw": raw}
        return _failure("empty", "the server returned no %s result" % operation)
    if status == 0:
        return _failure("unreachable", _transport_message(rt, payload))
    code = str(payload.get("code") or "") if isinstance(payload, dict) else ""
    if status == 422 and code in ("unknown_operation", "invalid_operation"):
        _store_capability(rt["service_url"], "op:" + operation, False)
        return _failure("unknown_operation", "this server has no %s CODE operation" % operation)
    return _failure(code or "http_%s" % status, "HTTP %s: %s" % (status, _detail(payload)[:300]))


def operation_known(rt: dict, operation: str) -> bool:
    """False only while the server is remembered as lacking the operation."""
    return _cached_capability(rt["service_url"], "op:" + operation) is not False
