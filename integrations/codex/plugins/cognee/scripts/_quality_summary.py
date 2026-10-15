"""Credential/dataset-bound evaluation cache. Refresh in the worker; rendering is pure local I/O."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

REFRESH_SECONDS = 3600
MAX_AGE_SECONDS = 48 * 3600


def cache_path(root: Path, base_url: str, *, dataset_id="", principal="") -> Path:
    identity = hashlib.sha256(
        json.dumps([base_url.rstrip("/"), dataset_id, principal]).encode()
    ).hexdigest()
    return root / "quality" / (identity + ".json")


def read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def validate(summary, now):
    if not isinstance(summary, dict) or summary.get("scope") not in {"benchmark", "personal"}:
        return "pending"
    if summary.get("status") == "pending":
        return "pending"
    if summary.get("schema") not in {1, 2} or summary.get("source") != "live":
        return "pending"
    if (summary.get("scope") == "personal") != (summary.get("schema") == 2):
        return "pending"
    keys = ("pairs", "better", "equivalent", "worse", "unjudgeable")
    if any(type(summary.get(k)) is not int or summary[k] < 0 for k in keys):
        return "pending"
    total = summary["pairs"]
    if total < 20 or summary["unjudgeable"] or sum(summary[k] for k in keys[1:]) != total:
        return "pending"
    for name, count in (("improvement_rate", "better"), ("regression_rate", "worse")):
        value = summary.get(name)
        if type(value) not in (int, float) or not math.isfinite(value):
            return "pending"
        if abs(value - summary[count] / total) > 1e-9:
            return "pending"
    try:
        stamp = datetime.fromisoformat(summary["evaluated_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.timestamp() > now + 300:
            return "pending"
    except (KeyError, TypeError, ValueError):
        return "pending"
    if now - stamp.timestamp() > MAX_AGE_SECONDS or summary.get("status") == "stale":
        return "stale"
    return "ready" if summary.get("status") == "ready" else "pending"


def refresh(root, base_url, fetch, *, now, dataset_id="", principal=""):
    """Never blocks recall; failed/unsupported endpoints are retried after one hour."""
    from _injection_ledger import atomic_json

    path = cache_path(root, base_url, dataset_id=dataset_id, principal=principal)
    old = read(path)
    checked = old.get("checked_at", 0)
    if type(checked) in (int, float) and 0 <= now - checked < REFRESH_SECONDS:
        return
    summary = old.get("summary", {})
    try:
        result = fetch()
        if isinstance(result, dict):
            summary = result
    except Exception as exc:
        if getattr(exc, "code", None) in {401, 403, 404}:
            summary = {}
    atomic_json(path, {"checked_at": now, "summary": summary})


def cache_scope(session_key=""):
    from _plugin_common import _api_key, resolve_active_dataset_ids
    try:
        dataset_id, _ = resolve_active_dataset_ids(session_key)
        principal = hashlib.sha256(_api_key().encode()).hexdigest()
    except (OSError, ValueError, RuntimeError):
        return {"dataset_id": "unavailable", "principal": "unavailable"}
    return {"dataset_id": dataset_id, "principal": principal}


def refresh_current():
    import time
    from urllib.parse import urlencode

    from _cloud_worker import root, target
    from _plugin_common import _json_http_request

    scope = cache_scope()
    if not scope["dataset_id"]:
        return
    path = "/api/v1/evaluations/summary?" + urlencode({"dataset_id": scope["dataset_id"]})
    refresh(
        root(),
        target()["base_url"],
        lambda: _json_http_request(path, method="GET", timeout=3),
        now=time.time(),
        **scope,
    )


def segment(root, base_url, *, now, compact=False, bar=True, dataset_id="", principal=""):
    summary = read(cache_path(root, base_url, dataset_id=dataset_id, principal=principal)).get(
        "summary", {}
    )
    state = validate(summary, now)
    score = 47
    if state in {"ready", "stale"}:
        score = round(
            47 + 53 * max(0, min(1, (summary["better"] - summary["worse"]) / summary["pairs"]))
        )
    filled = round(score / 100 * 5)
    blocks = " " + "▰" * filled + "▱" * (5 - filled) if bar else ""
    suffix = " · calibrating" if state == "pending" else " · stale" if state == "stale" else ""
    return f"Better answers{blocks} {score}%{suffix}"


if __name__ == "__main__":
    import time

    from _env_file import load_env_file

    load_env_file()
    host = "codex" if "codex" in Path(__file__).parts else "claude-code"
    from _plugin_common import _local_api_url

    url = _local_api_url()
    cached = read(cache_path(Path.home() / ".cognee-plugin" / host, url, **cache_scope())).get(
        "summary", {}
    )
    print(json.dumps({**cached, "status": validate(cached, time.time())}, indent=2))
