#!/usr/bin/env python3
"""Search session + trace + graph-context for context relevant to the user's prompt.

Runs on the Codex UserPromptSubmit hook. Calls ``cognee.recall`` with
``scope=["session","trace","graph_context"]`` so every layer the
SessionManager holds (QA entries, agent trace steps, and the distilled
graph-knowledge snapshot from ``improve()``) flows back into Codex's
context.

Configuration:
    Resolves session state via Cognee HTTP endpoints.
"""

import asyncio
import json
import os
import sys
import time

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    get_session_key,
    hook_log,
    load_resolved,
    mark_server_ready,
    notify,
    quiet_hook_output,
    read_and_reset_save_counter,
    recall_via_http,
    resolve_runtime_mode,
    resolve_session_key_from_payload,
    resolve_user,
    server_health_ok,
    server_ready_hint,
    set_session_key,
)
from cognee_statusline_render import render_status_for_host
from config import ensure_cognee_ready, get_session_id, load_config


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


TOP_K = 5
TRUNCATE_ANSWER = 500
TRUNCATE_RETURN = 400
TRUNCATE_GRAPH_CTX = 1500
RECENT_TRACE_FALLBACK_TOP_K = 5


def _load_session_id() -> str:
    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    if not session_id:
        config = load_config()
        session_id = get_session_id(config)
    return session_id


def _load_user_id() -> str:
    return load_resolved().get("user_id", "")


def _format_entry(entry: dict) -> str:
    """Format a single recall result according to its _source tag."""
    source = entry.get("source", "")

    if source == "graph_context":
        # graph_context entries carry `content`; graph_completion results
        # (folded in from scope=graph) carry `text`. Try both.
        content = str(entry.get("content", "") or entry.get("text", ""))[:TRUNCATE_GRAPH_CTX]
        return f"[graph-snapshot]\n{content}"

    if source == "session_context":
        content = str(entry.get("content", "") or entry.get("text", ""))[:TRUNCATE_GRAPH_CTX]
        return f"[agent-guidance]\n{content}"

    if source == "trace":
        origin = entry.get("origin_function", "?")
        status = entry.get("status", "")
        feedback = entry.get("session_feedback", "")
        mrv = entry.get("method_return_value", "")
        if isinstance(mrv, (dict, list)):
            mrv = json.dumps(mrv, default=str)
        mrv = str(mrv)[:TRUNCATE_RETURN]
        parts = [f"[trace] {origin} — {status}"]
        if feedback:
            parts.append(f"  feedback: {feedback}")
        if mrv:
            parts.append(f"  output: {mrv}")
        return "\n".join(parts)

    # session (QA) or generic
    q = entry.get("question", "")
    a = entry.get("answer", "")
    t = entry.get("time", "")
    lines = []
    if q:
        lines.append(f"[{t}] Q: {q}")
    if a:
        a_short = a[:TRUNCATE_ANSWER] + "..." if len(a) > TRUNCATE_ANSWER else a
        lines.append(f"A: {a_short}")
    return "\n".join(lines)


def _has_entry_content(entry: dict) -> bool:
    """Return True when a recall entry has useful content to inject."""
    source = entry.get("source", "")
    if source == "graph_context":
        return bool(str(entry.get("content", "") or entry.get("text", "")).strip())
    if source == "session_context":
        return bool(str(entry.get("content", "") or entry.get("text", "")).strip())
    if source == "trace":
        fields = ("origin_function", "status", "session_feedback", "method_return_value")
    else:
        fields = ("question", "answer")
    return any(str(entry.get(field, "") or "").strip() for field in fields)


async def _recent_trace_fallback(session_id: str, user_id: str, top_k: int) -> list[dict]:
    """Return recent trace rows directly when semantic trace recall misses.

    Tool calls are chronological session context, not only semantic context. A
    casual next prompt often will not match the words in a tool output, but the
    agent still needs to see the recent tool calls it just made.
    """
    try:
        from cognee.infrastructure.session.get_session_manager import get_session_manager

        sm = get_session_manager()
        if not sm.is_available or not user_id:
            return []
        raw_trace = await sm.get_agent_trace_session(user_id=user_id, session_id=session_id)
        entries = list(raw_trace or [])[-top_k:]
    except Exception as exc:
        hook_log("trace_fallback_error", {"error": str(exc)[:200]})
        return []

    normalized: list[dict] = []
    for entry in entries:
        if hasattr(entry, "model_dump"):
            entry = entry.model_dump()
        elif hasattr(entry, "dict"):
            entry = entry.dict()
        elif hasattr(entry, "__dict__"):
            entry = dict(entry.__dict__)
        if not isinstance(entry, dict):
            continue
        entry["source"] = "trace"
        if _has_entry_content(entry):
            normalized.append(entry)
    return normalized


async def _run(prompt: str) -> dict | None:
    config = load_config()
    runtime = resolve_runtime_mode()
    cloud_mode = runtime["mode"] == "http"
    # Readiness gate: never block the user's prompt on a warming/migrating
    # backend. Trust a fresh readiness marker (zero-network); on a miss, do one
    # short /health probe and record the result. If still not ready, skip recall
    # entirely so the prompt is answered at full speed (memory turns on later).
    service_url = runtime.get("base_url", "")
    if not server_ready_hint(service_url):
        if server_health_ok(service_url, timeout=_float_env("COGNEE_READY_PROBE_TIMEOUT", 1.0)):
            mark_server_ready(service_url)
        else:
            hook_log("recall_skipped_warming", {"base_url": service_url})
            return None

    if not cloud_mode:
        await ensure_cognee_ready(config)

    session_id = _load_session_id()
    if not session_id:
        hook_log("no_session_id", {"event": "context_lookup"})
        return None

    saves_last_turn = read_and_reset_save_counter(session_id)

    # Run scopes independently: a failure in one (e.g. graph search hitting an
    # empty/locked Ladybug DB) must not discard hits already collected from the
    # others. cognee.recall loops over scopes and re-raises on the first failure,
    # so we call it once per scope and collect whatever succeeds.
    results: list = []
    scope_specs = [
        (["session"], None, None),
        (["trace"], None, None),
        (["graph_context"], None, None),
        (["graph"], "HYBRID_COMPLETION", None),
        (["session_context"], None, "agent"),
    ]
    if not cloud_mode:
        import cognee
        from cognee.modules.search.types import SearchType

        user = await resolve_user(_load_user_id())

    # Hard time-box: this hook is on the keystroke->answer path, so recall must
    # never be the long pole. Each scope gets a short per-call timeout, and the
    # whole loop stops once the overall budget is spent. Partial results are fine.
    recall_timeout = _float_env("COGNEE_RECALL_TIMEOUT", 2.5)
    budget_deadline = time.monotonic() + _float_env("COGNEE_RECALL_BUDGET", 4.0)
    # Respect the shared circuit breaker: when the server has been failing (tripped
    # by the explicit recall path), skip this per-prompt recall rather than hammering
    # a down backend on every keystroke. HTTP/cloud mode only.
    if cloud_mode:
        try:
            from _cognee_client import breaker_open

            _bopen, _bretry = breaker_open()
        except Exception:
            _bopen, _bretry = False, 0
        if _bopen:
            hook_log("recall_breaker_open", {"retry_in": _bretry})
            scope_specs = []
    for scope_list, qtype, context_profile in scope_specs:
        if time.monotonic() >= budget_deadline:
            hook_log("recall_budget_exceeded", {"collected": len(results)})
            break
        try:
            if cloud_mode:
                part = recall_via_http(
                    prompt,
                    session_id=session_id,
                    top_k=TOP_K,
                    scope=scope_list,
                    only_context=True,
                    search_type=qtype,
                    context_profile=context_profile,
                    timeout=recall_timeout,
                )
            else:
                query_type = getattr(SearchType, qtype, None) if qtype else None
                part = await asyncio.wait_for(
                    cognee.recall(
                        prompt,
                        session_id=session_id,
                        top_k=TOP_K,
                        scope=scope_list,
                        only_context=True,
                        query_type=query_type,
                        user=user,
                        **({"context_profile": context_profile} if context_profile else {}),
                    ),
                    timeout=recall_timeout,
                )
            if part:
                results.extend(part)
        except Exception as exc:
            hook_log("recall_error", {"scope": scope_list, "error": str(exc)[:200]})

    # Bucket results by _source for human-readable output.
    # Local SDK mode returns Pydantic models (ResponseQAEntry, etc.); cloud
    # mode returns plain dicts via HTTP. Normalize to dicts here.
    by_source: dict[str, list] = {
        "session": [],
        "trace": [],
        "graph_context": [],
        "session_context": [],
    }
    for r in results or []:
        if hasattr(r, "model_dump"):
            r = r.model_dump()
        if not isinstance(r, dict):
            continue
        src = r.get("source", "session")
        # Fold scope=graph (HYBRID_COMPLETION) results into the graph_context
        # bucket so the displayed `g` counter reflects what was retrieved.
        if src == "graph":
            r["source"] = "graph_context"
            src = "graph_context"
        if not _has_entry_content(r):
            continue
        by_source.setdefault(src, []).append(r)

    if not cloud_mode and not by_source.get("trace"):
        fallback_traces = await _recent_trace_fallback(
            session_id,
            _load_user_id(),
            RECENT_TRACE_FALLBACK_TOP_K,
        )
        if fallback_traces:
            by_source["trace"].extend(fallback_traces)
            hook_log("trace_fallback_hit", {"count": len(fallback_traces)})

    counts = {k: len(v) for k, v in by_source.items()}
    total = sum(counts.values())

    # Write last-turn counts so the status line script can render them.
    # Best-effort; failure here must not break the hook output.
    try:
        from pathlib import Path as _Path

        _state = _Path.home() / ".cognee-plugin" / "codex" / "last_recall.json"
        _state.parent.mkdir(parents=True, exist_ok=True)
        _state.write_text(
            json.dumps(
                {
                    "session_id": session_id,
                    "ts": __import__("datetime")
                    .datetime.now(__import__("datetime").timezone.utc)
                    .isoformat(timespec="seconds"),
                    "hits": counts,
                    "saves_last_turn": saves_last_turn,
                }
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        hook_log("last_recall_write_failed", {"error": str(exc)[:200]})

    # Build a visibility header so the user (via the assistant's
    # context) can tell that memory fired on this turn — both what it
    # recalled right now and what the previous turn persisted.
    status_line = render_status_for_host(get_session_key())
    header = (
        f"{status_line}\n"
        "Cognee memory: recall "
        f"{counts['session']} session / {counts['trace']} trace / "
        f"{counts['graph_context']} graph / {counts['session_context']} agent; saved last turn "
        f"{saves_last_turn['prompt']} prompt / {saves_last_turn['trace']} trace / "
        f"{saves_last_turn['answer']} answer"
    )

    if total > 0:
        hook_log("context_lookup_hit", {"counts": counts, "saves_last_turn": saves_last_turn})
        notify(f"recall hit ({counts}); saves last turn {saves_last_turn}")
    else:
        hook_log("context_lookup_empty", {"saves_last_turn": saves_last_turn})
        notify(f"no recall matches; saves last turn {saves_last_turn}")

    # full_context for the audit log — build sections but don't surface them in
    # the terminal. Only the header (status + counts) is shown to the user.
    section_lines = []
    if by_source.get("session_context"):
        section_lines.append("=== Active agent guidance ===")
        for e in by_source["session_context"]:
            section_lines.append(_format_entry(e))
            section_lines.append("")
    if by_source.get("graph_context"):
        section_lines.append("=== Knowledge graph snapshot ===")
        for e in by_source["graph_context"]:
            section_lines.append(_format_entry(e))
            section_lines.append("")
    if by_source.get("trace"):
        section_lines.append("=== Prior agent trace ===")
        for e in by_source["trace"]:
            section_lines.append(_format_entry(e))
            section_lines.append("")
    if by_source.get("session"):
        section_lines.append("=== Prior session turns ===")
        for e in by_source["session"]:
            section_lines.append(_format_entry(e))
            section_lines.append("")
    full_context = f"{header}\n\n" + "\n".join(section_lines).strip() if section_lines else header

    # Audit log: persist full recall details per turn for debugging.
    # Only the header (status line + counts) is returned as additionalContext;
    # the full sections go to the audit log only.
    try:
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        from pathlib import Path as _Path

        _audit = _Path.home() / ".cognee-plugin" / "codex" / "recall-audit.log"
        _audit.parent.mkdir(parents=True, exist_ok=True)
        with _audit.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": _dt.now(_tz.utc).isoformat(timespec="seconds"),
                        "session_id": session_id,
                        "prompt": prompt,
                        "hits": counts,
                        "context": full_context,
                    }
                )
                + "\n"
            )
    except Exception as exc:
        hook_log("recall_audit_write_failed", {"error": str(exc)[:200]})

    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": header,
        }
    }


def main():
    payload_raw = sys.stdin.read()
    if not payload_raw.strip():
        return

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return

    session_key_candidate, session_key_source = resolve_session_key_from_payload(payload)
    if session_key_candidate:
        set_session_key(session_key_candidate)
    hook_log(
        "context_lookup_session_key", {"source": session_key_source, "value": session_key_candidate}
    )
    if not get_session_key():
        hook_log("context_lookup_missing_session_key")
        return

    prompt = payload.get("prompt", "")
    if not prompt or len(prompt) < 5:
        return

    output = None
    try:
        with quiet_hook_output("session-context-lookup"):
            output = asyncio.run(_run(prompt))
    except Exception as exc:
        hook_log("context_lookup_exception", {"error": str(exc)[:200]})
    if output:
        print(json.dumps(output))


if __name__ == "__main__":
    main()
