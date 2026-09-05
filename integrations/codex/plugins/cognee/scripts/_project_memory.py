"""Opt-in project tags and server-verified companion routing, pinned per session."""

import hashlib
import os
import re
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


def prepare(dataset: str, session_id: str) -> dict:
    from _plugin_common import _json_http_request, _load_json_file, _write_json_file

    path = _path(dataset, session_id)
    state = _load_json_file(path)
    if not state or not state.get("pending"):
        return state
    state["identity"] = _identity()
    state["pending"] = False
    state["write"] = dataset
    try:
        if state.get("node_set"):
            schema = _json_http_request("/openapi.json", None, method="GET", timeout=5)
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
            rows = _json_http_request("/api/v1/datasets", None, method="GET", timeout=5)
            matches = [row for row in rows or [] if row.get("name") == dataset]
            if len(matches) != 1:
                raise ValueError("Primary dataset is missing or ambiguous")
            primary_id = matches[0]["id"]
            result = _json_http_request(
                f"/api/v1/datasets/{primary_id}/session-companion", {}, timeout=15
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
        # No local SQL queries and no speculative suffix routing on any failure.
        state["fallback"] = type(error).__name__
        if state.get("node_set") and "schema" not in locals():
            state["error"] = "Project tag capability could not be verified; capture remains queued"
    _write_json_file(path, state)
    return state


def route(dataset: str, session_id: str) -> dict:
    from _plugin_common import _load_json_file

    state = _load_json_file(_path(dataset, session_id))
    if not state:
        return {"primary": dataset, "write": dataset, "node_set": []}
    if state.get("pending"):
        raise RuntimeError("Project memory provisioning is pending")
    if state.get("identity") != _identity():
        raise RuntimeError("Project memory principal changed; restart the session")
    if state.get("error"):
        raise RuntimeError(state["error"])
    return state
