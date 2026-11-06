"""Opt-in project tags and server-verified companion routing, pinned per session."""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path


def project_tag(cwd: str, setting: str) -> str:
    if not setting or setting.lower() in ("0", "false", "off", "no"):
        return ""
    if setting.lower() != "auto":
        if len(setting) > 200 or not setting.strip():
            raise ValueError("Project node set must contain 1–200 characters")
        return setting.strip()
    canonical = str(Path(cwd).resolve())
    label = re.sub(r"[^a-zA-Z0-9_-]", "-", Path(canonical).name)[:60] or "project"
    return "project-" + label + "-" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _path(dataset: str, session_id: str) -> Path:
    from _plugin_common import _PLUGIN_DIR, resolved_http_endpoint_auth

    url, _ = resolved_http_endpoint_auth()
    key = hashlib.sha256(
        (url.rstrip("/") + "\n" + dataset + "\n" + session_id).encode()
    ).hexdigest()
    return _PLUGIN_DIR / "project-memory" / (key + ".json")


def _identity() -> str:
    from _plugin_common import resolved_http_endpoint_auth

    _, key = resolved_http_endpoint_auth()
    return hashlib.sha256(key.encode()).hexdigest()


def begin(dataset: str, session_id: str, cwd: str) -> None:
    from _plugin_common import _load_json_file, _write_json_file

    setting = os.environ.get("COGNEE_PROJECT_NODE_SET", "")
    companion = os.environ.get("COGNEE_SESSION_COMPANION_DATASET", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    companion = (
        companion and dataset != "agent_sessions" and not dataset.endswith("-agent_sessions")
    )
    if setting.lower() == "auto":
        try:
            from _coding_memory import enabled as coding_enabled
            from _coding_memory import git_root

            if coding_enabled():
                # Coding memory scopes lessons per repository: tag the repository
                # root, not the subdirectory the session happened to open in.
                cwd = git_root(cwd) or cwd
        except Exception:
            pass
    tag = project_tag(cwd, setting)
    if not tag and not companion:
        return
    path = _path(dataset, session_id)
    if _load_json_file(path):
        return
    _write_json_file(
        path,
        {
            "pending": True,
            "primary": dataset,
            "write": dataset,
            "node_set": [tag] if tag else [],
            "companion": companion,
        },
    )


# A probe that could not reach the server (timeout, connection error, a reply
# that is not JSON, a gateway error) is retried on this schedule instead of
# being persisted as a permanent error: 30 s, 2 min, then every 10 min.
_RETRY_BACKOFF_SECONDS = (30.0, 120.0, 600.0)
PENDING_NOTICE = (
    "Cognee couldn't confirm that the server stores project tags, so this session's "
    "memory is queued and will be sent once it can."
)


def _retry_delay(attempts: int) -> float:
    return _RETRY_BACKOFF_SECONDS[min(max(attempts, 1), len(_RETRY_BACKOFF_SECONDS)) - 1]


def _transport_failure(error: BaseException) -> bool:
    """True when the server never gave an answer to act on (retry later).

    An HTTP status is an answer, except a throttle or a server/gateway error,
    which says nothing about what the server supports.
    """
    import http.client
    import json
    import urllib.error

    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or error.code >= 500
    return isinstance(
        error, (urllib.error.URLError, OSError, http.client.HTTPException, json.JSONDecodeError)
    )


def prepare(dataset: str, session_id: str, timeout: float = 5.0) -> dict:
    """Confirm, once per session, what this session's writes may carry.

    A spec without ``node_set`` is a permanent error (capture stays queued); an
    explicit companion refusal falls back to the primary dataset. A probe the
    server never answered leaves the state pending with a ``retry_at``, and
    ``route`` tries again once it has passed.
    """
    from _plugin_common import _json_http_request, _load_json_file, _write_json_file

    path = _path(dataset, session_id)
    state = _load_json_file(path)
    if not state or not state.get("pending"):
        return state
    state["identity"] = _identity()
    state["write"] = dataset
    spec_read = False
    try:
        if state.get("node_set"):
            schema = _json_http_request("/openapi.json", None, method="GET", timeout=timeout)
            spec_read = True
            models = (
                schema.get("components", {}).get("schemas", {}) if isinstance(schema, dict) else {}
            )
            if not all(
                "node_set" in models.get(name, {}).get("properties", {})
                for name in ("QAEntry", "TraceEntry")
            ):
                state["error"] = (
                    "Backend does not support project node sets; capture remains queued"
                )
        if state.get("companion"):
            rows = _json_http_request("/api/v1/datasets", None, method="GET", timeout=timeout)
            matches = [row for row in rows or [] if row.get("name") == dataset]
            if len(matches) != 1:
                raise ValueError("Primary dataset is missing or ambiguous")
            primary_id = matches[0]["id"]
            result = _json_http_request(
                f"/api/v1/datasets/{primary_id}/session-companion", {}, timeout=timeout * 3
            )
            if (
                isinstance(result, dict)
                and result.get("permissions_verified") is True
                and result.get("primary_dataset_id") == primary_id
                and result.get("dataset_name") == dataset + "-agent_sessions"
                and result.get("dataset_id")
            ):
                state["write"] = result["dataset_name"]
            else:
                raise ValueError("Backend did not attest companion permissions")
    except Exception as error:
        tag_unverified = bool(state.get("node_set")) and not spec_read
        if tag_unverified or _transport_failure(error):
            # Unanswered: keep holding the entries, and try again later.
            attempts = int(state.get("attempts", 0) or 0) + 1
            state["attempts"] = attempts
            state["retry_at"] = time.time() + _retry_delay(attempts)
            state["last_failure"] = type(error).__name__
            state["write"] = dataset
            _write_json_file(path, state)
            return state
        # An explicit refusal. No local SQL queries and no speculative suffix
        # routing: fall back to the primary dataset.
        state["fallback"] = type(error).__name__
        state["write"] = dataset
    state["pending"] = False
    for key in ("retry_at", "last_failure"):
        state.pop(key, None)
    _write_json_file(path, state)
    return state


# The route-triggered retry runs inside a hook, so it waits less than session start.
ROUTE_PREPARE_TIMEOUT = 2.0


def route(dataset: str, session_id: str) -> dict:
    from _plugin_common import _load_json_file

    state = _load_json_file(_path(dataset, session_id))
    if not state:
        return {"primary": dataset, "write": dataset, "node_set": []}
    if state.get("pending"):
        # retry_at is absent when prepare never ran (cloud registration failed).
        retry_at = state.get("retry_at")
        if not isinstance(retry_at, (int, float)) or time.time() >= retry_at:
            state = prepare(dataset, session_id, timeout=ROUTE_PREPARE_TIMEOUT) or state
        if state.get("pending"):
            raise RuntimeError("Project memory provisioning is pending; capture remains queued")
    if state.get("identity") != _identity():
        raise RuntimeError("Project memory principal changed; restart the session")
    if state.get("error"):
        raise RuntimeError(state["error"])
    return state


def pending_notice(dataset: str, session_id: str) -> str:
    """The one-time note while a failed probe keeps this session's memory queued, or ''."""
    from _plugin_common import _load_json_file, _write_json_file

    path = _path(dataset, session_id)
    state = _load_json_file(path)
    if not state or not state.get("pending") or not state.get("attempts"):
        return ""
    if state.get("pending_noticed"):
        return ""
    state["pending_noticed"] = True
    _write_json_file(path, state)
    return PENDING_NOTICE
