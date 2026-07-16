#!/usr/bin/env python3
"""Build a memory anchor before context-window compaction.

Runs on the PreCompact hook. Pulls a compact summary from already-stored
session-cache layers — recent QAs and per-step trace feedback — and emits a
markdown block the compactor preserves.

PreCompact intentionally does not run live graph search: there is no real user
query at compact time, and deriving one from recalled/compacted context can feed
synthetic text back into Cognee as if it were a user question.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    get_session_key,
    hook_log,
    load_resolved,
    quiet_hook_output,
    recall_via_http,
    resolve_session_key_from_payload,
    resolve_user,
    set_session_key,
)
from config import (
    ensure_cognee_ready,
    ensure_dataset_ready,
    get_dataset,
    get_session_id,
    is_cloud_mode,
    load_config,
)

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


def _as_dict(entry):
    if hasattr(entry, "model_dump"):
        try:
            return entry.model_dump()
        except Exception as exc:
            hook_log("precompact_model_dump_failed", {"error": str(exc)[:200]})
    if hasattr(entry, "dict"):
        try:
            return entry.dict()
        except Exception as exc:
            hook_log("precompact_dict_dump_failed", {"error": str(exc)[:200]})
    if hasattr(entry, "__dict__"):
        return dict(entry.__dict__)
    return entry


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


async def _recall(
    session_id: str,
    dataset: str,
    query: str,
    scope: list[str],
    top_k: int,
    config: dict,
    user=None,
) -> list:
    """Thin wrapper around cognee.recall; tolerates empty/failed recalls."""
    try:
        if is_cloud_mode(config):
            qtype = "GRAPH_COMPLETION" if "graph" in scope else None
            results = recall_via_http(
                query,
                session_id=session_id,
                top_k=top_k,
                scope=scope,
                only_context=True,
                search_type=qtype,
            )
        else:
            import cognee
            from cognee.modules.search.types import SearchType

            query_type = SearchType.GRAPH_COMPLETION if "graph" in scope else None
            results = await cognee.recall(
                query,
                session_id=session_id,
                datasets=[dataset] if "graph" in scope else None,
                top_k=top_k,
                scope=scope,
                query_type=query_type,
                user=user,
            )
        return list(results) if results else []
    except Exception as exc:
        hook_log("precompact_recall_error", {"scope": scope, "error": str(exc)[:200]})
        return []


def _format_session_section(entries: list) -> str:
    lines = ["### Session Memory (recent turns)"]
    for entry in entries:
        entry = _as_dict(entry)
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
        entry = _as_dict(entry)
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

    config = load_config()
    await ensure_cognee_ready(config)
    user = None
    if not is_cloud_mode(config):
        user = await resolve_user(user_id)
        await ensure_dataset_ready(dataset, user)

    # Short queries: use the session's recent activity as the seed
    # since we don't have a specific user question at compact time.
    # First pull session+trace so we can derive a query from them.
    seed_results = await _recall(
        session_id,
        dataset,
        query="",
        scope=["session", "trace"],
        top_k=_TRACE_TOP_K,
        config=config,
        user=user,
    )
    normalized_seed = [_as_dict(r) for r in seed_results]
    session_entries = [
        r
        for r in normalized_seed
        if isinstance(r, dict) and (r.get("source") or r.get("_source")) == "session"
    ]
    trace_entries = [
        r
        for r in normalized_seed
        if isinstance(r, dict) and (r.get("source") or r.get("_source")) == "trace"
    ]

    # Fall back: if recall returned nothing (keyword-miss on empty query),
    # pull entries directly. This keeps the anchor useful mid-session
    # before any user prompts have landed in the cache.
    if not session_entries and not trace_entries:
        try:
            from cognee.infrastructure.session.get_session_manager import get_session_manager

            resolved = load_resolved()
            user_id = resolved.get("user_id", "")
            if user_id:
                sm = get_session_manager()
                if sm.is_available:
                    raw_qa = await sm.get_session(
                        user_id=user_id, session_id=session_id, formatted=False
                    )
                    session_entries = list(raw_qa)[-_SESSION_TOP_K:] if raw_qa else []
                    raw_trace = await sm.get_agent_trace_session(
                        user_id=user_id, session_id=session_id
                    )
                    trace_entries = list(raw_trace)[-_TRACE_TOP_K:] if raw_trace else []
        except Exception as exc:
            hook_log("precompact_direct_fetch_error", {"error": str(exc)[:200]})

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
