"""Turn a tapes session export payload into a readable transcript.

Extraction rules follow the tapes exporter integration (PR #362): only "main"
LLM spans become transcript text — injected system context, permission-check
offshoots, and other harness-internal spans are skipped, and tool calls are
summarized down to a curated set of argument keys so large diffs/file contents
don't bloat the graph.
"""

# Argument keys worth surfacing per tool call, tried in order; the first
# group with any match wins.
KEYS = [
    ("command",),
    ("skill",),
    ("subagent_type", "prompt"),
    ("file_path",),
    ("filePath",),
    ("notebook_path",),
    ("cron", "recurring"),
    ("channel_id", "team"),
    ("query",),
    ("url",),
    ("message",),
    ("taskId", "subject", "description"),
    ("status",),
]
MAX_KEY_LENGTH = 80


def get_status(session: dict) -> str:
    rollup = session.get("session", {}).get("rollup") or {}
    return rollup.get("status", "")


def summarize_tool_input(tool_input: object) -> str:
    if not isinstance(tool_input, dict):
        return ""

    for key_group in KEYS:
        matched = [k for k in key_group if k in tool_input]
        if matched:
            parts = []
            for key in matched:
                value = str(tool_input[key])
                if len(value) > MAX_KEY_LENGTH:
                    value = value[:MAX_KEY_LENGTH] + "..."
                parts.append(f"{key}: {value}")
            return f"({', '.join(parts)})"
    return ""


def _read_trace(session: dict) -> str:
    session_text = ""
    traces = session.get("traces", []) if isinstance(session, dict) else []

    for trace_entry in traces:
        if not isinstance(trace_entry, dict):
            continue

        trace = trace_entry.get("trace", {})
        spans = trace_entry.get("spans", [])
        if not isinstance(trace, dict):
            continue

        turn_text = f"User: {trace.get('user_prompt', '')}\n\n"

        main_spans = [
            span
            for span in spans
            if isinstance(span, dict)
            and span.get("kind") == "llm"
            and span.get("call_kind") == "main"
        ]
        main_spans.sort(key=lambda s: s.get("seq", 0))

        assistant_parts = []
        for span in main_spans:
            for block in span.get("output", []):
                if not isinstance(block, dict):
                    continue

                block_type = block.get("type")
                if block_type == "text":
                    if text := block.get("text", ""):
                        assistant_parts.append(text)
                elif block_type == "tool_use":
                    tool_name = block.get("tool_name", "unknown")
                    detail = summarize_tool_input(block.get("tool_input", {}))
                    assistant_parts.append(f"[used tool: {tool_name}{detail}]")

        turn_text += "Assistant: " + "\n".join(assistant_parts) + "\n\n"
        session_text += turn_text

    return session_text


def build_transcript(session: dict) -> str:
    """Header + conversational turns; empty string when there is nothing to say."""
    meta = session.get("session", {})
    rollup = meta.get("rollup") or {}
    body = _read_trace(session)
    if not body.strip():
        return ""

    header = (
        f"Session ID: {meta.get('id', '')}\n"
        f"Name: {meta.get('name', '')}\n"
        f"Display title: {meta.get('display_title', '')}\n"
        f"Harness: {meta.get('harness_id', '')}\n"
        f"Started: {meta.get('started_at', '')}\n"
        f"Model: {rollup.get('model', '')}\n\n"
    )
    return header + body
