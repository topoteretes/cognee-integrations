#!/usr/bin/env python3
"""Search session + trace + graph-context for context relevant to the user's prompt.

Runs on the UserPromptSubmit hook. Calls ``cognee.recall`` with
``scope=["session","trace","graph_context"]`` so every layer the
SessionManager holds (QA entries, agent trace steps, and the distilled
graph-knowledge snapshot from ``improve()``) flows back into Claude's
context.

Configuration:
    Uses resolved session ID from SessionStart hook (via ~/.cognee-plugin/resolved.json).
"""

import asyncio
import json
import os
import sys

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import hook_log, load_resolved, notify
from config import ensure_cognee_ready, get_session_id, load_config

TOP_K = 3
TRUNCATE_ANSWER = 500
TRUNCATE_RETURN = 400
TRUNCATE_GRAPH_CTX = 1500


def _load_session_id() -> str:
    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    if not session_id:
        config = load_config()
        session_id = get_session_id(config)
    return session_id


def _format_entry(entry: dict) -> str:
    """Format a single recall result according to its _source tag."""
    source = entry.get("_source", "")

    if source == "graph_context":
        content = str(entry.get("content", ""))[:TRUNCATE_GRAPH_CTX]
        return f"[graph-snapshot]\n{content}"

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


async def _run(prompt: str):
    import cognee

    config = load_config()
    await ensure_cognee_ready(config)

    session_id = _load_session_id()
    if not session_id:
        hook_log("no_session_id", {"event": "context_lookup"})
        return

    try:
        results = await cognee.recall(
            prompt,
            session_id=session_id,
            top_k=TOP_K,
            scope=["session", "trace", "graph_context"],
        )
    except Exception as exc:
        hook_log("recall_error", {"error": str(exc)[:200]})
        return

    if not results:
        notify("no session matches")
        hook_log("context_lookup_empty")
        return

    # Bucket results by _source for human-readable output.
    by_source: dict[str, list] = {"session": [], "trace": [], "graph_context": []}
    for r in results:
        if not isinstance(r, dict):
            continue
        src = r.get("_source", "session")
        by_source.setdefault(src, []).append(r)

    section_lines = []
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

    if not section_lines:
        return

    context = "Relevant context from this session's memory:\n\n" + "\n".join(section_lines).strip()
    counts = {k: len(v) for k, v in by_source.items() if v}
    hook_log("context_lookup_hit", {"counts": counts})
    notify(f"injected context ({counts})")

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    print(json.dumps(output))


def main():
    payload_raw = sys.stdin.read()
    if not payload_raw.strip():
        return

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return

    prompt = payload.get("prompt", "")
    if not prompt or len(prompt) < 5:
        return

    try:
        asyncio.run(_run(prompt))
    except Exception as exc:
        hook_log("context_lookup_exception", {"error": str(exc)[:200]})


if __name__ == "__main__":
    main()
