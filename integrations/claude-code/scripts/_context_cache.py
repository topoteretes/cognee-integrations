"""Distilled project digest: worker writes, SessionStart reads without networking."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import time
from pathlib import Path

from _cloud_worker import read, root, target
from _injection_budget import fit_sections
from _injection_ledger import atomic_json, entry_hash, is_guidance_section, seed

ENTRY_FORMAT = "bare-records-v1"
TTL = 24 * 3600
REFRESH_INTERVAL = 30 * 60
#: A refresh that keeps failing is dropped; the next improve or active session
#: asks again, and the last good digest stays in place meanwhile.
MAX_REFRESH_ATTEMPTS = 5
QUERY = "project decisions conventions pitfalls user preferences and open tasks"
RAW_HISTORY = re.compile(
    r"Previous conversation|Session ID:|(?:^|\n)(?:Question:|Answer:|Q:|A:)", re.I
)


def cache_path(dataset: str) -> Path:
    from _plugin_common import dataset_id_for

    identity = dataset_id_for(dataset) or dataset
    safe = (
        identity
        if re.fullmatch(r"[\w.-]+", identity)
        else hashlib.sha256(identity.encode()).hexdigest()
    )
    return root() / "context-cache" / (safe + ".json")


def request_refresh(
    dataset: str, session_id: str = "", host_key: str = "", *, record_usage: bool = False
) -> None:
    from _plugin_common import get_session_key

    path = cache_path(dataset).with_suffix(".wanted.json")
    # A pending usage stamp survives a later plain refresh request.
    record_usage = record_usage or bool(read(path).get("record_usage"))
    atomic_json(
        path,
        {
            "dataset": dataset,
            "session_id": session_id,
            "host_key": host_key or get_session_key(),
            "target": target(),
            "requested_at": time.time(),
            "record_usage": record_usage,
        },
    )


def load(dataset: str, *, now: float = None) -> dict:
    data = read(cache_path(dataset))
    age = (time.time() if now is None else now) - data.get("built_at", 0)
    if (
        data.get("entry_format") != ENTRY_FORMAT
        or data.get("target") != target()
        or age < 0
        or age > TTL
    ):
        return {}
    if RAW_HISTORY.search(str(data.get("rendered", ""))):
        return {}
    return data


def inject(dataset: str, session_key: str, *, source: str = "") -> str:
    if source == "resume":
        return ""
    data = load(dataset)
    if not data:
        request_refresh(dataset, host_key=session_key)
        return ""
    rendered = str(data.get("rendered") or "")
    if not rendered:
        return ""
    seed(session_key, data.get("entry_hashes", []), data.get("guidance_hashes", []))
    from _session_stats import update

    update(session_key, digest_items_used=len(data.get("entry_hashes", [])))
    if data.get("guidance_hashes"):
        # Guidance reached a session: let the worker's next refresh stamp it as
        # used. Background rebuilds alone never count as use.
        request_refresh(dataset, data.get("source_session_id", ""), session_key, record_usage=True)
    return rendered


def _sections(rows: list, session_id: str) -> list:
    # Share exact rendering with per-prompt recall, so ledger hashes agree.
    spec = importlib.util.spec_from_file_location(
        "digest_recall_renderer", Path(__file__).with_name("session-context-lookup.py")
    )
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    from _recall_text import trim_recall_text

    buckets = {"graph_context": [], "session_context": []}
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = row.get("source")
        if source == "session_context":
            content = str(row.get("content") or row.get("text") or "")
            if content and not RAW_HISTORY.search(content):
                buckets[source].append({**row, "content": content})
        elif source == "graph":
            content, _, _ = trim_recall_text(str(row.get("text") or ""), QUERY, session_id)
            # Fail closed on unexpected transcript content, even if a server ignored the filter.
            if content and not RAW_HISTORY.search(content):
                buckets["graph_context"].append({"source": "graph_context", "text": content})
    return renderer._injection_sections(buckets)


def build(
    dataset: str, session_id: str, host_key: str, *, request=None, record_usage: bool = False
) -> dict:
    from _plugin_common import _json_http_request, dataset_id_for, read_connection_state
    from _session_stats import previous, summary
    from _session_stats import read as read_stats

    request = request or _json_http_request
    from _dataset_access import write_fields

    scoped = write_fields(dataset_id_for(dataset) or dataset)
    fields = (
        {"dataset_ids": [scoped["dataset_id"]]}
        if "dataset_id" in scoped
        else {"datasets": [dataset]}
    )
    version = read_connection_state().get("version", "")
    capabilities = []
    try:
        health = request("/health", None, method="GET", timeout=4)
        if isinstance(health, dict):
            version = str(health.get("version") or version)
            capabilities = health.get("capabilities") or []
            atomic_json(
                root() / "server-capabilities.json",
                {
                    "version": version,
                    "capabilities": capabilities,
                    "target": target(),
                    "checked_at": time.time(),
                },
            )
    except Exception:
        pass
    base = {"query": QUERY, **fields, "top_k": 5, "only_context": True}
    rows = request(
        "/api/v1/recall",
        {
            **base,
            "scope": ["graph"],
            "search_type": "HYBRID_COMPLETION",
            "node_name": ["session_learnings", "project_docs"],
        },
        timeout=8,
    )
    rows = list(rows) if isinstance(rows, list) else []
    if session_id:
        guidance = request(
            "/api/v1/recall",
            {
                **base,
                "scope": ["session_context"],
                "session_id": session_id,
                "context_profile": "agent",
                "record_context_usage": record_usage and "context_usage_v1" in capabilities,
            },
            timeout=8,
        )
        if isinstance(guidance, list):
            rows.extend(guidance)
    sections = _sections(rows, session_id)
    if "session_summary_v1" in capabilities:
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("session_summary"), dict):
                summary_data = row["session_summary"]
                paragraph = " ".join(
                    label + ": " + ("; ".join(value) if isinstance(value, list) else str(value))
                    for key, label in (
                        ("goal", "Goal"),
                        ("outcome", "Outcome"),
                        ("decisions", "Decisions"),
                        ("open_items", "Open items"),
                    )
                    for value in [summary_data.get(key)]
                    if value
                )
                if paragraph and not RAW_HISTORY.search(paragraph):
                    sections.insert(
                        0, {"title": "=== Last session ===", "entries": [paragraph], "priority": 45}
                    )
                break
    current = read_stats(host_key)
    last = summary(
        current if current.get("ended_at") else previous(host_key, dataset, target()["base_url"])
    )
    header = "Cognee project memory — distilled guidance and project facts."
    if last:
        header += "\n" + last
    rendered, stats = fit_sections([{"entries": [header], "required": True}] + sections, 6000)
    kept = set(stats["entries"])
    data = {
        "entry_format": ENTRY_FORMAT,
        "rendered": rendered,
        "entry_hashes": [entry_hash(entry) for entry in stats["entries"]],
        "guidance_hashes": [
            entry_hash(entry)
            for section in sections
            if is_guidance_section(section)
            for entry in section["entries"]
            if entry in kept
        ],
        "built_at": time.time(),
        "server_version": version,
        "target": target(),
        "dataset": dataset,
        "source_session_id": session_id,
    }
    atomic_json(cache_path(dataset), data)
    return data


def refresh_pending() -> None:
    """Called only by the worker; failed reads preserve the last good cache.

    Failures are logged and back off exponentially. A request for another
    account, older than the cache TTL, or past MAX_REFRESH_ATTEMPTS is removed,
    so it cannot keep the worker from idling out.
    """
    from _cloud_worker import log
    from _plugin_common import set_session_key

    now = time.time()
    for path in (root() / "context-cache").glob("*.wanted.json"):
        item = read(path)
        if not item or item.get("target") != target():
            _drop(path, "other_target")
            continue
        if now - item.get("requested_at", 0) > TTL:
            _drop(path, "expired")
            continue
        if now < item.get("next_attempt", 0):
            continue
        set_session_key(item.get("host_key", ""))
        try:
            cached = load(item["dataset"])
            sid = item.get("session_id") or cached.get("source_session_id", "")
            build(
                item["dataset"],
                sid,
                item.get("host_key", ""),
                record_usage=bool(item.get("record_usage")),
            )
            # A request arriving during refresh must survive.
            if read(path).get("requested_at") == item.get("requested_at"):
                path.unlink()
        except Exception as exc:
            attempts = int(item.get("attempts", 0)) + 1
            log(
                "context_cache_refresh_failed",
                dataset=item.get("dataset", ""),
                attempts=attempts,
                error=type(exc).__name__,
            )
            if read(path).get("requested_at") != item.get("requested_at"):
                continue
            if attempts >= MAX_REFRESH_ATTEMPTS:
                _drop(path, "attempts")
                continue
            item.update(attempts=attempts, next_attempt=now + min(3600, 60 * 2 ** (attempts - 1)))
            atomic_json(path, item)


def _drop(path: Path, reason: str) -> None:
    from _cloud_worker import log

    path.unlink(missing_ok=True)
    log("context_cache_refresh_dropped", file=path.name, reason=reason)


def schedule_active_refreshes() -> None:
    now = time.time()
    for path in (root() / "sessions").glob("*/worker.json"):
        state = read(path)
        if state.get("target") != target() or now - state.get("activity_at", 0) > REFRESH_INTERVAL:
            continue
        dataset = state["dataset"]
        cached = load(dataset)
        wanted = cache_path(dataset).with_suffix(".wanted.json")
        if now - cached.get("built_at", 0) >= REFRESH_INTERVAL and not wanted.exists():
            request_refresh(dataset, cached.get("source_session_id", ""), state["session_key"])
