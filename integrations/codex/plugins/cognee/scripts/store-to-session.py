#!/usr/bin/env python3
"""Store tool calls and assistant responses into the Cognee session cache.

Routes tool calls to the structured ``TraceEntry`` path (new trace-step
shape with origin_function / method_params / method_return_value /
status). Routes the final assistant message on Stop to a ``QAEntry``.

Runs async on the PostToolUse / Stop hooks - fire-and-forget, never
blocks Codex.

Configuration:
    Resolves session state via Cognee HTTP endpoints.
"""

import asyncio
import json
import os
import sys

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    append_http_bridge_entry,
    append_warmup_entry,
    bump_save_counter,
    bump_turn_counter,
    get_session_key,
    hook_log,
    http_api_ready,
    load_resolved,
    notify,
    pop_pending_prompt,
    quiet_hook_output,
    remember_entry_via_http,
    resolve_runtime_mode,
    resolve_session_key_from_payload,
    resolve_user,
    run_session_improve,
    server_ready_hint,
    set_session_key,
    touch_activity,
)
from config import (
    ensure_cognee_ready,
    ensure_dataset_ready,
    get_dataset,
    get_session_id,
    improve_session_local,
    load_config,
)

# Hard cap per field to avoid ballooning the cache with massive tool outputs.
_MAX_PARAMS_BYTES = 4000
_MAX_RETURN_BYTES = 8000
_MAX_ASSISTANT_BYTES = 8000


async def _fire_improve_background(dataset: str, session_id: str, user, reason: str) -> None:
    """Fire-and-forget session improve; failures are logged but never raised.

    The server bridges the session itself from its session cache (improve),
    instead of the old client-side full-document re-post — see run_session_improve.
    """
    try:
        if http_api_ready():
            wrote = run_session_improve(dataset, session_id)
            hook_log(
                "auto_improve_fired",
                {"reason": reason, "session": session_id, "via": "http_improve", "wrote": wrote},
            )
            if wrote:
                notify(f"session improve submitted ({reason})")
            return

        await ensure_dataset_ready(dataset, user)
        result = await improve_session_local(dataset, session_id, user)
        hook_log(
            "auto_improve_fired",
            {
                "reason": reason,
                "session": session_id,
                "via": "local_improve",
                "ok": bool(result.get("ok")),
            },
        )
        notify(f"session improve completed ({reason})")
    except Exception as exc:
        hook_log("auto_improve_error", {"reason": reason, "error": str(exc)[:200]})


def _truncate_str(value, cap: int) -> str:
    """Coerce to string and cap at ``cap`` bytes (utf-8), appending ``...`` if truncated."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= cap:
        return text
    return encoded[: cap - 3].decode("utf-8", errors="ignore") + "..."


def _infer_status(payload: dict) -> tuple[str, str]:
    """Return (status, error_message) from a PostToolUse payload."""
    # Codex and Claude-style payloads may set tool_response.is_error=True on failures; also
    # check for an explicit 'error' key at the top level.
    response = payload.get("tool_response") or payload.get("tool_output") or ""
    if isinstance(response, dict):
        if response.get("is_error") or response.get("error"):
            err = response.get("error") or response.get("message") or "Tool reported an error."
            return "error", _truncate_str(err, 500)
    if isinstance(payload.get("error"), str) and payload["error"]:
        return "error", _truncate_str(payload["error"], 500)
    return "success", ""


def _load_session(config: dict, *, use_http: bool) -> tuple[str, str, str]:
    """Load session metadata without network I/O in latency-sensitive HTTP hooks."""
    if use_http:
        return get_session_id(config), get_dataset(config), ""

    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    dataset = resolved.get("dataset", "")
    user_id = resolved.get("user_id", "")
    if not session_id or not dataset:
        session_id = session_id or get_session_id(config)
        dataset = dataset or get_dataset(config)
    return session_id, dataset, user_id


async def _store_tool_call(payload: dict) -> None:
    """Write a PostToolUse event as a TraceEntry."""
    tool_name = payload.get("tool_name", "unknown")
    tool_input = payload.get("tool_input") or {}
    tool_output = payload.get("tool_output") or payload.get("tool_response") or ""

    # Suppress self-reference: any Bash call that mentions 'cognee' is
    # likely the plugin/CLI talking to itself and would recurse.
    if tool_name == "Bash":
        cmd = ""
        if isinstance(tool_input, dict):
            cmd = str(tool_input.get("command", ""))
        if "cognee" in cmd:
            hook_log("skip_self_cognee_bash", {"cmd_prefix": cmd[:80]})
            return

    status, error_message = _infer_status(payload)

    # Normalize method_params: small structured dict is ideal; fall back
    # to a truncated-string dict if we got something non-JSON-safe.
    if isinstance(tool_input, dict):
        params = {}
        for k, v in tool_input.items():
            params[k] = _truncate_str(v, _MAX_PARAMS_BYTES)
    else:
        params = {"value": _truncate_str(tool_input, _MAX_PARAMS_BYTES)}

    return_value = _truncate_str(tool_output, _MAX_RETURN_BYTES)

    config = load_config()
    runtime = resolve_runtime_mode()
    use_http = runtime["mode"] == "http"
    session_id, dataset, user_id = _load_session(config, use_http=use_http)
    if not session_id:
        hook_log("no_session_id", {"tool": tool_name})
        return

    entry = {
        "type": "trace",
        "origin_function": tool_name,
        "status": status,
        "method_params": params,
        "method_return_value": return_value,
        "error_message": error_message,
        # LLM-backed feedback per step is expensive on a busy session —
        # fall back to the deterministic one-liner. Users who want the
        # LLM summary can flip this in a future config.
        "generate_feedback_with_llm": False,
    }

    if not server_ready_hint(runtime.get("base_url", "")):
        # Server still warming: don't block the tool call and don't lose the
        # trace. Buffer the structured entry for a later /remember/entry replay
        # (improve bridges only what the server session cache holds), and keep
        # the legacy text mirror for the document-bridge fallback path.
        append_warmup_entry(dataset, session_id, entry)
        trace_text = (
            f"{tool_name} [{status}]\n"
            f"Params: {json.dumps(params, ensure_ascii=False)}\n"
            f"Return: {return_value}"
        )
        append_http_bridge_entry(dataset, session_id, trace=trace_text)
        bump_save_counter(session_id, "trace")
        hook_log("store_buffered_warming", {"hook": "tool", "tool": tool_name})
        return
    if not use_http:
        await ensure_cognee_ready(config)

    try:
        if use_http:
            result = remember_entry_via_http(dataset, session_id, entry)
            user = None
        else:
            import cognee
            from cognee.memory import TraceEntry

            user = await resolve_user(user_id)
            result = await cognee.remember(
                TraceEntry(**entry),
                dataset_name=dataset,
                session_id=session_id,
                self_improvement=False,
                user=user,
            )
    except Exception as exc:
        hook_log("trace_store_error", {"tool": tool_name, "error": str(exc)[:200]})
        notify(f"trace store failed ({exc})")
        return

    if result:
        trace_id = (
            result.get("entry_id")
            if isinstance(result, dict)
            else getattr(result, "entry_id", None)
        )
        hook_log(
            "trace_stored",
            {
                "tool": tool_name,
                "status": status,
                "trace_id": trace_id,
            },
        )
        notify(f"trace stored ({tool_name}, {status})")
        if use_http:
            trace_text = (
                f"{tool_name} [{status}]\n"
                f"Params: {json.dumps(params, ensure_ascii=False)}\n"
                f"Return: {return_value}"
            )
            append_http_bridge_entry(
                dataset,
                session_id,
                trace=trace_text,
            )
        bump_save_counter(session_id, "trace")

        touch_activity()
        count, should_improve = bump_turn_counter(session_id)
        if should_improve:
            await _fire_improve_background(dataset, session_id, user, reason=f"turn_{count}")
    else:
        hook_log("trace_store_noresult", {"tool": tool_name})


async def _store_assistant_stop(payload: dict) -> None:
    """Write a Stop-hook payload (final assistant message) as a QAEntry."""
    msg = str(payload.get("assistant_message") or payload.get("last_assistant_message") or "")
    if not msg or msg == "null":
        return

    msg = _truncate_str(msg, _MAX_ASSISTANT_BYTES)

    config = load_config()
    runtime = resolve_runtime_mode()
    use_http = runtime["mode"] == "http"
    session_id, dataset, user_id = _load_session(config, use_http=use_http)
    if not session_id:
        hook_log("no_session_id", {"event": "stop"})
        return

    pending = pop_pending_prompt(session_id, turn_id=str(payload.get("turn_id") or ""))

    # Codex intentionally differs from Claude here: store one paired
    # prompt/answer row so Cognee's filesystem session cache does not get
    # separate question-only and answer-only QA entries for the same turn.
    entry = {
        "type": "qa",
        "question": pending.get("prompt", ""),
        "answer": msg,
        "context": pending.get("context", ""),
    }

    if not server_ready_hint(runtime.get("base_url", "")):
        # Server still warming: buffer the structured entry for a later
        # /remember/entry replay (improve bridges only what the server session
        # cache holds), and keep the legacy text mirror for the document-bridge
        # fallback path.
        append_warmup_entry(dataset, session_id, entry)
        append_http_bridge_entry(
            dataset,
            session_id,
            question=pending.get("prompt", ""),
            answer=msg,
        )
        bump_save_counter(session_id, "answer")
        hook_log("store_buffered_warming", {"hook": "stop"})
        return
    if not use_http:
        await ensure_cognee_ready(config)

    try:
        if use_http:
            result = remember_entry_via_http(dataset, session_id, entry)
            user = None
        else:
            import cognee
            from cognee.memory import QAEntry

            user = await resolve_user(user_id)
            result = await cognee.remember(
                QAEntry(**entry),
                dataset_name=dataset,
                session_id=session_id,
                self_improvement=False,
                user=user,
            )
    except Exception as exc:
        hook_log("stop_store_error", {"error": str(exc)[:200]})
        notify(f"stop store failed ({exc})")
        return

    if result:
        if use_http:
            append_http_bridge_entry(
                dataset,
                session_id,
                question=pending.get("prompt", ""),
                answer=msg,
            )
        qa_id = (
            result.get("entry_id")
            if isinstance(result, dict)
            else getattr(result, "entry_id", None)
        )
        hook_log("stop_stored", {"chars": len(msg), "qa_id": qa_id})
        notify(f"assistant message stored ({len(msg)} chars)")
        bump_save_counter(session_id, "answer")

        touch_activity()
        count, should_improve = bump_turn_counter(session_id)
        if should_improve:
            await _fire_improve_background(dataset, session_id, user, reason=f"turn_{count}")


def main():
    payload_raw = sys.stdin.read()
    if not payload_raw.strip():
        return

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        hook_log("invalid_payload_json")
        return

    session_key_candidate, session_key_source = resolve_session_key_from_payload(payload)
    if session_key_candidate:
        set_session_key(session_key_candidate)
    hook_log("store_session_key", {"source": session_key_source, "value": session_key_candidate})
    if not get_session_key():
        hook_log("store_missing_session_key")
        return

    is_stop = "--stop" in sys.argv
    try:
        with quiet_hook_output("store-to-session"):
            if is_stop:
                asyncio.run(_store_assistant_stop(payload))
            else:
                asyncio.run(_store_tool_call(payload))
    except Exception as exc:
        hook_log("run_exception", {"stop": is_stop, "error": str(exc)[:200]})


if __name__ == "__main__":
    main()
