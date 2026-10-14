"""Tenant-bound benchmark cache. Refresh in the worker; rendering is pure local I/O."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

REFRESH_SECONDS = 3600
MAX_AGE_SECONDS = 48 * 3600


def cache_path(root: Path, base_url: str) -> Path:
    identity = hashlib.sha256(base_url.rstrip("/").encode()).hexdigest()
    return root / "quality" / (identity + ".json")


def read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def validate(summary, now):
    if not isinstance(summary, dict) or summary.get("scope") != "benchmark":
        return "pending"
    if summary.get("status") == "pending":
        return "pending"
    if summary.get("schema") != 1 or summary.get("source") != "live":
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


def refresh(root, base_url, fetch, *, now):
    """Never blocks recall; failed/unsupported endpoints are retried after one hour."""
    from _injection_ledger import atomic_json

    path = cache_path(root, base_url)
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


def segment(root, base_url, *, now, compact=False, bar=True):
    summary = read(cache_path(root, base_url)).get("summary", {})
    state = validate(summary, now)
    label = "Improved" if compact else "Answer improvements"
    if state != "ready":
        return f"{label}: {state}"
    rate, regression = summary["improvement_rate"], summary["regression_rate"]
    # Five cells share the existing status-line palette. This replaces the imprint bar.
    filled = round(rate * 5)
    blocks = " " + "▰" * filled + "▱" * (5 - filled) if bar else ""
    worse = f" · {regression:.0%} worse" if regression else ""
    return f"{label}{blocks} {rate:.0%} · benchmark{worse}"


if __name__ == "__main__":
    import os
    import time

    from _env_file import load_env_file

    load_env_file()
    host = "codex" if "codex" in Path(__file__).parts else "claude-code"
    url = os.environ.get("COGNEE_BASE_URL", "")
    cached = read(cache_path(Path.home() / ".cognee-plugin" / host, url)).get("summary", {})
    print(json.dumps({**cached, "status": validate(cached, time.time())}, indent=2))
