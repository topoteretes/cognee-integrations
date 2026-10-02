#!/usr/bin/env python3
"""Build a memory anchor before context-window compaction.

Runs on the PreCompact hook. Pulls a compact summary from three
session-cache layers — recent QAs, per-step trace feedback, and the
graph-context snapshot — and emits a markdown block the compactor
preserves.

Uses ``cognee.recall(scope=[...])`` so this works whether the plugin
is connected locally or over HTTP.
"""

import asyncio
import json
import os
import re
import sys

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import normalize_hook_payload
from _plugin_common import (
    hook_log,
    load_resolved,
    recall_via_http,
    resolve_session_key_from_payload,
    set_session_key,
)
from config import ensure_cognee_ready, get_dataset, get_session_id, is_cloud_mode, load_config

_MIN_WORD_LEN = 3
_SESSION_TOP_K = 5
_TRACE_TOP_K = 8
_GRAPH_TOP_K = 3


def _load_resolved_fields() -> tuple[str, str]:
    """Return (session_id, dataset) from resolved cache or config."""
    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    dataset = resolved.get("dataset", "")
    if not session_id or not dataset:
        config = load_config()
        session_id = session_id or get_session_id(config)
        dataset = dataset or get_dataset(config)
    return session_id, dataset


def _as_dict(entry):
    """Coerce one recall result to a plain dict.

    The two backends return different shapes: the local SDK can hand back pydantic
    models, while ``recall_via_http`` returns already-decoded JSON. Normalising here
    means the filters below can just read keys.
    """
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


def _extract_query_words(entries: list, max_words: int = 20) -> str:
    """Pull keyword-dense query from recent entries for graph-context search."""
    words: list[str] = []
    for entry in entries[-3:]:
        if not isinstance(entry, dict):
            continue
        blob = " ".join(
            str(entry.get(f, ""))
            for f in ("question", "answer", "origin_function", "session_feedback")
        )
        for w in re.findall(r"\b\w+\b", blob.lower()):
            if len(w) >= _MIN_WORD_LEN:
                words.append(w)
                if len(words) >= max_words:
                    return " ".join(words)
    return " ".join(words)


async def _recall(
    session_id: str,
    dataset: str,
    query: str,
    scope: list[str],
    top_k: int,
    config: dict,
) -> list:
    """Recall for the anchor, over HTTP in server mode and the SDK locally.

    Server mode is not optional here: the session cache lives on the server, so
    ``cognee.recall`` has nothing local to read and every scope comes back empty.
    Without this branch the hook logs ``precompact_empty`` and prints nothing —
    silently, since a compaction is not something the user triggered — so anyone
    running against a server loses their anchor at exactly the moment compaction
    throws the transcript away.

    Mirrors codex's pre-compact, which has had the branch since it was written.
    """
    try:
        if is_cloud_mode(config):
            # GRAPH_COMPLETION only for the graph scope; the session/trace scopes
            # read the cache and must not force a graph query.
            query_type = "GRAPH_COMPLETION" if "graph" in scope else None
            results = recall_via_http(
                query,
                session_id=session_id,
                top_k=top_k,
                scope=scope,
                only_context=True,
                search_type=query_type,
                dataset=get_dataset(config),
            )
        else:
            import cognee

            results = await cognee.recall(
                query,
                session_id=session_id,
                datasets=[dataset] if "graph" in scope else None,
                top_k=top_k,
                scope=scope,
            )
        return list(results) if results else []
    except Exception as exc:
        hook_log("precompact_recall_error", {"scope": scope, "error": str(exc)[:200]})
        return []


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


def _format_graph_context_section(entries: list) -> str:
    lines = ["### Knowledge Graph Snapshot"]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or entry.get("answer") or entry.get("text") or "")
        short = content[:400] + "..." if len(content) > 400 else content
        if short.strip():
            lines.append(short)
    return "\n".join(lines) if len(lines) > 1 else ""


def _format_graph_section(entries: list) -> str:
    lines = ["### Knowledge Graph (search hits)"]
    for entry in entries:
        if not isinstance(entry, dict):
            lines.append(f"- {str(entry)[:300]}")
            continue
        text = entry.get("answer") or entry.get("text") or entry.get("content") or str(entry)
        short = (text[:300] + "...") if len(text) > 300 else text
        lines.append(f"- {short}")
    return "\n".join(lines) if len(lines) > 1 else ""


async def _run():
    session_id, dataset = _load_resolved_fields()
    if not session_id:
        hook_log("no_session_id", {"event": "precompact"})
        return

    config = load_config()
    await ensure_cognee_ready(config)

    # Short queries: use the session's recent activity as the seed
    # since we don't have a specific user question at compact time.
    # First pull session+trace so we can derive a query from them.
    seed_results = await _recall(
        session_id, dataset, query="", scope=["session", "trace"], top_k=_TRACE_TOP_K, config=config
    )
    # The two backends tag the origin scope differently: recall_via_http returns
    # `source`, the local SDK returns `_source`. Reading only `_source` silently
    # filtered out every HTTP result, which is why adding the transport branch
    # alone still produced an empty anchor — the recall worked and its results were
    # then discarded here.
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
    # NOT guarded by is_cloud_mode, deliberately. The seed recall above passes an
    # empty query (there is no user question at compact time) and the server's
    # /recall matches nothing on an empty string — verified directly: query="" -> 0
    # results, query="ZEBRA-1" -> 1. So over HTTP the seed is always empty and this
    # fallback is the only thing that produces an anchor.
    #
    # It works for a *local* server because the plugin boots it under the same HOME,
    # so the session manager reads the very storage the server writes. Against a
    # remote tenant the import fails and the except swallows it, leaving no anchor —
    # the real remaining gap, which needs a server-side way to read recent session
    # entries without a query rather than more client-side branching.
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

    query = _extract_query_words(session_entries + trace_entries)

    graph_context_entries: list = []
    graph_entries: list = []
    if query:
        ctx = await _recall(
            session_id, dataset, query=query, scope=["graph_context"], top_k=1, config=config
        )
        graph_context_entries = [d for d in (_as_dict(r) for r in ctx) if isinstance(d, dict)]
        g = await _recall(
            session_id, dataset, query=query, scope=["graph"], top_k=_GRAPH_TOP_K, config=config
        )
        graph_entries = [d for d in (_as_dict(r) for r in g) if isinstance(d, dict)]

    sections = []
    if session_entries:
        s = _format_session_section(session_entries)
        if s:
            sections.append(s)
    if trace_entries:
        s = _format_trace_section(trace_entries)
        if s:
            sections.append(s)
    if graph_context_entries:
        s = _format_graph_context_section(graph_context_entries)
        if s:
            sections.append(s)
    if graph_entries:
        s = _format_graph_section(graph_entries)
        if s:
            sections.append(s)

    if not sections:
        hook_log("precompact_empty")
        return

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
            "graph_context": len(graph_context_entries),
            "graph": len(graph_entries),
        },
    )
    print(anchor)


def main():
    # Read the PreCompact payload to recover the host session id, which lets the
    # session resolver map back to this launch's Cognee session id (the body is
    # otherwise unused — PreCompact is just a trigger).
    payload_raw = sys.stdin.read()
    try:
        payload = normalize_hook_payload(json.loads(payload_raw) if payload_raw.strip() else {})
    except json.JSONDecodeError:
        payload = {}
    session_key_candidate, _ = resolve_session_key_from_payload(payload)
    if session_key_candidate:
        set_session_key(session_key_candidate)

    try:
        asyncio.run(_run())
    except Exception as exc:
        hook_log("precompact_run_exception", {"error": str(exc)[:200]})


if __name__ == "__main__":
    main()
