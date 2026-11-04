#!/usr/bin/env python3
"""Build a memory anchor before context-window compaction.

Runs on the PreCompact hook. Pulls a compact summary from already-stored
session-cache layers — recent QAs and per-step trace feedback — and emits a
markdown block the compactor preserves.

PreCompact intentionally does not run live graph search: there is no real user
query at compact time, and deriving one from recalled/compacted context can feed
synthetic text back into Cognee as if it were a user question.

Everything goes through the Cognee server over HTTP (``/api/v1/recall`` and
``GET /api/v1/sessions/{id}``), so the anchor works the same whether the
plugin booted a local server or is connected to a remote one.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    get_session_detail_via_http,
    get_session_key,
    hook_log,
    load_resolved,
    quiet_hook_output,
    recall_via_http,
    resolve_runtime_mode,
    resolve_session_key_from_payload,
    server_usable,
    set_session_key,
)
from config import get_dataset, get_session_id, load_config

_SESSION_TOP_K = 5
_TRACE_TOP_K = 8
_SYNC_SCRIPT = Path(__file__).with_name("sync-session-to-graph.py")
_DETACHED_SYNC_ARG = "--detached-final"
_SYNC_START_DELAY_SECONDS = "2"


def _load_resolved_fields() -> tuple[str, str, str]:
    """Return (session_id, dataset, user_id) from runtime endpoint state or config."""
    if not get_session_key():
        hook_log("precompact_missing_session_key")
        return "", "", ""
    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    dataset = resolved.get("dataset", "")
    user_id = resolved.get("user_id", "")
    if not session_id or not dataset:
        config = load_config()
        session_id = session_id or get_session_id(config)
        dataset = dataset or get_dataset(config)
    return session_id, dataset, user_id


def _spawn_background_sync(session_id: str, dataset: str, user_id: str) -> None:
    """Kick off session-to-graph sync without blocking the PreCompact hook."""
    try:
        env = os.environ.copy()
        env.setdefault("COGNEE_SYNC_START_DELAY", _SYNC_START_DELAY_SECONDS)
        subprocess.Popen(
            [sys.executable, str(_SYNC_SCRIPT), _DETACHED_SYNC_ARG],
            cwd=os.getcwd(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        hook_log(
            "precompact_sync_deferred",
            {"session": session_id, "dataset": dataset, "user_id": user_id},
        )
    except Exception as exc:
        hook_log(
            "precompact_sync_defer_failed",
            {"session": session_id, "dataset": dataset, "error": str(exc)[:300]},
        )


def _recall(session_id: str, dataset: str, query: str, scope: list[str], top_k: int) -> list:
    """Recall for the anchor over HTTP; tolerates empty/failed recalls."""
    try:
        results = recall_via_http(
            query,
            session_id=session_id,
            top_k=top_k,
            scope=scope,
            only_context=True,
            dataset=dataset,
        )
        return [r for r in (results or []) if isinstance(r, dict)]
    except Exception as exc:
        hook_log("precompact_recall_error", {"scope": scope, "error": str(exc)[:200]})
        return []


def _recent_entries(session_id: str) -> tuple[list, list]:
    """Return (recent QA entries, recent trace entries) straight from the server.

    The seed recall passes an empty query (there is no user question at compact
    time) and ``/recall`` matches nothing on an empty string, so the session
    detail endpoint — which returns the last ~20 QA and trace rows without a
    query — is what actually produces the anchor mid-session.
    """
    detail = get_session_detail_via_http(session_id)
    if not isinstance(detail, dict):
        return [], []
    qas = [r for r in (detail.get("qas") or []) if isinstance(r, dict)]
    traces = [r for r in (detail.get("traces") or []) if isinstance(r, dict)]
    return qas[-_SESSION_TOP_K:], traces[-_TRACE_TOP_K:]


def _format_session_section(entries: list) -> str:
    lines = ["### Session Memory (recent turns)"]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        q = str(entry.get("question") or "").strip()
        a = str(entry.get("answer") or "").strip()
        if not (q or a):
            continue
        short = (q or a)[:300]
        if len(q or a) > 300:
            short += "..."
        prefix = "Q: " if q else "A: "
        lines.append(f"- {prefix}{short}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _format_trace_section(entries: list) -> str:
    lines = ["### Agent Trace (tool calls & feedback)"]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        origin = entry.get("origin_function", "?")
        status = entry.get("status", "")
        feedback = str(entry.get("session_feedback") or "").strip()
        if feedback:
            lines.append(f"- {origin} [{status}]: {feedback[:200]}")
        else:
            lines.append(f"- {origin} [{status}]")
    return "\n".join(lines) if len(lines) > 1 else ""


async def _run():
    session_id, dataset, user_id = _load_resolved_fields()
    if not session_id:
        hook_log("no_session_id", {"event": "precompact"})
        return ""
    hook_log("precompact_start", {"session": session_id, "dataset": dataset, "user_id": user_id})

    # Pin the endpoint the same way every other hook does (URL + optional key
    # into the environment), then bail early on a server already known to be
    # down rather than paying recall timeouts for nothing.
    runtime = resolve_runtime_mode()
    service_url = runtime.get("base_url", "")
    if not server_usable(service_url):
        hook_log("precompact_server_unusable", {"base_url": service_url})
        return ""

    # Seed: the session's recent activity, since there is no user question at
    # compact time. Try recall first, then the session detail endpoint, which
    # returns the recent rows without needing a query.
    seed_results = _recall(
        session_id, dataset, query="", scope=["session", "trace"], top_k=_TRACE_TOP_K
    )
    session_entries = [r for r in seed_results if r.get("source") == "session"]
    trace_entries = [r for r in seed_results if r.get("source") == "trace"]
    if not session_entries and not trace_entries:
        session_entries, trace_entries = _recent_entries(session_id)

    session_entries = session_entries[-_SESSION_TOP_K:]
    trace_entries = trace_entries[-_TRACE_TOP_K:]

    sections = []
    if session_entries:
        s = _format_session_section(session_entries)
        if s:
            sections.append(s)
    if trace_entries:
        s = _format_trace_section(trace_entries)
        if s:
            sections.append(s)

    if not sections:
        hook_log("precompact_empty")
        _spawn_background_sync(session_id, dataset, user_id)
        return ""

    header = (
        "## Cognee Memory Anchor\n"
        "Preserved context from session, agent trace, and knowledge graph:\n"
    )
    anchor = header + "\n\n".join(sections)

    hook_log(
        "precompact_anchor",
        {
            "session_entries": len(session_entries),
            "trace_entries": len(trace_entries),
        },
    )
    _spawn_background_sync(session_id, dataset, user_id)
    return anchor


def main():
    # Read the PreCompact payload to recover the host session id, which lets the
    # session resolver map back to this launch's Cognee session id (the body is
    # otherwise unused — PreCompact is just a trigger).
    payload_raw = sys.stdin.read()
    try:
        payload = json.loads(payload_raw) if payload_raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    session_key_candidate, _ = resolve_session_key_from_payload(payload)
    if session_key_candidate:
        set_session_key(session_key_candidate)

    anchor = ""
    try:
        with quiet_hook_output("pre-compact"):
            anchor = asyncio.run(_run())
    except Exception as exc:
        hook_log("precompact_run_exception", {"error": str(exc)[:200]})
    if anchor:
        print(anchor)


if __name__ == "__main__":
    main()
