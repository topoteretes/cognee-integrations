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

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _cloud_worker import enabled as cloud_enabled
from _plugin_common import (
    append_warmup_entry,
    bump_save_counter,
    bump_turn_counter,
    clear_payment_required,
    elapsed_ms,
    get_session_key,
    hook_log,
    http_api_ready,
    improve_throttle_reason,
    load_resolved,
    notify,
    pop_pending_prompt,
    quiet_hook_output,
    record_payment_required,
    remember_entry_via_http,
    resolve_runtime_mode,
    resolve_session_key_from_payload,
    run_session_improve_detailed,
    server_usable,
    set_session_key,
    touch_activity,
    write_outcome_ambiguous,
)
from config import get_dataset, get_session_id, load_config

# Hard cap per field to avoid ballooning the cache with massive tool outputs.
_MAX_PARAMS_BYTES = 4000
_MAX_RETURN_BYTES = 8000
_MAX_ASSISTANT_BYTES = 8000


async def _fire_improve_background(dataset: str, session_id: str, reason: str) -> None:
    """Fire-and-forget session improve; failures are logged but never raised.

    The server bridges the session itself from its session cache (improve);
    see run_session_improve_detailed. Shares the cooldown / backoff gate with the
    idle watcher; the session-end sync ignores it and covers whatever a skip
    here leaves behind. Without server auth there is nothing to submit to —
    the session-end sync picks the session up once a key is available.
    """
    if cloud_enabled():
        from _cloud_worker import request_improve

        request_improve(dataset, session_id, reason)
        return
    improve_start = time.monotonic()
    throttled = improve_throttle_reason(session_id)
    if throttled:
        hook_log(
            "auto_improve_throttled",
            {"reason": reason, "session": session_id, "why": throttled},
        )
        return
    try:
        if not http_api_ready():
            hook_log("auto_improve_skipped_no_auth", {"reason": reason, "session": session_id})
            return
        outcome = run_session_improve_detailed(dataset, session_id, trigger="auto")
        wrote = bool(outcome.get("ok"))
        hook_log(
            "auto_improve_fired",
            {
                "reason": reason,
                "session": session_id,
                "via": "http_improve",
                "wrote": wrote,
                "outcome": str(outcome.get("reason") or ""),
                "elapsed_ms": elapsed_ms(improve_start),
            },
        )
        if wrote:
            notify(f"session improve submitted ({reason})")
    except Exception as exc:
        # Emit elapsed_ms on the failure path too, so time-to-failure stays visible.
        hook_log(
            "auto_improve_error",
            {"reason": reason, "error": str(exc)[:200], "elapsed_ms": elapsed_ms(improve_start)},
        )


def _truncate_str(value, cap: int) -> str:
    """Coerce to string and cap at ``cap`` bytes (utf-8), appending ``...`` if truncated.

    Always round-trips through utf-8 with errors="replace": hook payloads can
    carry lone surrogates (binary tool output rendered into the transcript),
    and one stored surrogate 500s the session-detail endpoint and wedges the
    improve pipeline server-side.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= cap:
        return encoded.decode("utf-8")
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
        if response.get("interrupted"):
            return "error", "Tool interrupted."
        exit_code = response.get("exit_code", response.get("exitCode", 0))
        if exit_code not in (0, "0", None):
            return "error", "Tool exited with code %s." % exit_code
    if isinstance(payload.get("error"), str) and payload["error"]:
        return "error", _truncate_str(payload["error"], 500)
    return "success", ""


def _load_session() -> tuple[str, str, str]:
    """Load session_id, dataset, user_id from resolved cache with fallbacks."""
    # Local fields only: the identity probes cost up to 10s each on a slow
    # backend, on every tool call and Stop, and no store path uses user_id.
    resolved = load_resolved(identity=False)
    session_id = resolved.get("session_id", "")
    dataset = resolved.get("dataset", "")
    user_id = resolved.get("user_id", "")
    if not session_id or not dataset:
        config = load_config()
        session_id = session_id or get_session_id(config)
        dataset = dataset or get_dataset(config)
    return session_id, dataset, user_id


def _low_signal_trace(tool: str, output, status: str) -> bool:
    if status != "success":
        return False
    if tool in {"Read", "Grep", "Glob"}:
        return True
    if tool != "Bash" or status != "success":
        return False
    if isinstance(output, dict):
        if output.get("exit_code", 0) or output.get("exitCode", 0) or output.get("interrupted"):
            return False
        metadata = {"stdout", "stderr", "exit_code", "exitCode", "interrupted", "is_error"}
        return (
            set(output) <= metadata
            and not str(output.get("stdout") or "").strip()
            and not str(output.get("stderr") or "").strip()
        )
    return output is None or (isinstance(output, str) and not output.strip())


async def _store_tool_call(payload: dict) -> None:
    """Write a PostToolUse event as a TraceEntry."""
    tool_name = payload.get("tool_name", "unknown")
    shell_tool = tool_name in {
        "Bash",
        "exec_command",
        "functions.exec_command",
        "shell_command",
        "shell",
    }
    tool_input = payload.get("tool_input") or {}
    tool_output = payload.get("tool_output") or payload.get("tool_response") or ""
    command_for_evaluation = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if "cognee-search" in str(command_for_evaluation):
        try:
            from _evaluation_capture import remember_context

            remember_context(_truncate_str(tool_output, 16000))
        except Exception as exc:
            hook_log("evaluation_context_error", {"error": type(exc).__name__})

    from _capture_policy import (
        CapturePatternError,
        allow_tool,
        is_plugin_invocation,
        low_signal_bash,
        redact,
        safe_first_token,
    )

    try:
        allowed = allow_tool(tool_name, tool_input)
    except CapturePatternError as exc:
        # Fail closed, but not silently: this hook's output is never shown, so
        # the recall hook repeats the problem to the user on the next prompt.
        hook_log("capture_tools_invalid", {"tool": tool_name, "error": str(exc)[:200]})
        notify(f"trace not captured: {exc}")
        return
    if not allowed:
        return
    tool_input = redact(tool_input)
    tool_output = redact(tool_output)
    payload = redact(payload)

    command = (
        str(tool_input.get("command") or tool_input.get("cmd") or "")
        if isinstance(tool_input, dict)
        else ""
    )
    status, error_message = _infer_status(payload)
    reason = is_plugin_invocation(command) if shell_tool else ""
    low_signal = shell_tool and low_signal_bash(command, tool_output, status)
    if (
        shell_tool
        and isinstance(tool_output, dict)
        and str(tool_output.get("output") or "").strip()
    ):
        low_signal = False
    low_signal = low_signal or (
        cloud_enabled() and not shell_tool and _low_signal_trace(tool_name, tool_output, status)
    )
    from _session_stats import update

    if shell_tool:
        hook_log(
            "bash_capture_decision",
            {
                "host_session_id": get_session_key(),
                "outcome": "skipped_self"
                if reason
                else "skipped_low_signal"
                if low_signal
                else "captured",
            },
        )
        update(
            get_session_key(),
            bash_post_tool_use=1,
            bash_skipped_self=int(bool(reason)),
            bash_skipped_low_signal=int(not reason and low_signal),
            bash_traces_captured=int(not reason and not low_signal),
        )
    if reason or low_signal:
        update(
            get_session_key(),
            traces_skipped=1,
            traces_skipped_self=int(bool(reason)),
            traces_skipped_low_signal=int(not reason),
        )
    if reason:
        hook_log(
            "skip_self_cognee_bash", {"first_token": safe_first_token(command), "reason": reason}
        )
        return
    if low_signal:
        hook_log(
            "trace_skipped_low_signal",
            {"tool": tool_name, "first_token": safe_first_token(command)},
        )
        return

    # Normalize method_params: small structured dict is ideal; fall back
    # to a truncated-string dict if we got something non-JSON-safe.
    if isinstance(tool_input, dict):
        params = {}
        for k, v in tool_input.items():
            params[k] = _truncate_str(v, _MAX_PARAMS_BYTES)
    else:
        params = {"value": _truncate_str(tool_input, _MAX_PARAMS_BYTES)}

    return_value = _truncate_str(tool_output, _MAX_RETURN_BYTES)

    session_id, dataset, _user_id = _load_session()
    if not session_id:
        hook_log("no_session_id", {"tool": tool_name})
        return

    runtime = resolve_runtime_mode()
    hook_log(
        "mode_decision",
        {
            "hook": "store-to-session:tool",
            "mode": runtime["mode"],
            "base_url": runtime.get("base_url", ""),
            "url_source": runtime.get("url_source", ""),
            "key_source": runtime.get("key_source", ""),
            "api_key_present": runtime.get("api_key_present", False),
        },
    )
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

    if cloud_enabled(runtime):
        from _cloud_worker import enqueue

        enqueue(dataset, session_id, entry, "trace")
        bump_save_counter(session_id, "trace", buffered=True)
        return

    if not server_usable(runtime.get("base_url", "")):
        # Server unreachable (stale marker AND a failed probe — a stale marker
        # alone no longer buffers; this hook fires per tool call, so its probe
        # keeps the ready marker fresh through long turns, #298): don't block
        # the tool call and don't lose the trace. Buffer the structured entry
        # for a later /remember/entry replay (improve bridges only what the
        # server session cache holds).
        append_warmup_entry(dataset, session_id, entry)
        bump_save_counter(session_id, "trace", buffered=True)
        hook_log("store_buffered_warming", {"hook": "tool", "tool": tool_name})
        return

    try:
        result = remember_entry_via_http(dataset, session_id, entry)
    except Exception as exc:
        # Same reasoning as the Stop path: the server_usable() guard above only
        # catches an outage already known about, so a server that dies inside the
        # ready marker's 30s TTL lands here with a real, failed write. Buffer the
        # retryable cases so the trace survives; drop a 4xx loudly, because the
        # drain stops at the first entry it cannot send and a permanently
        # rejected one would block every entry behind it.
        status_code = exc.code if isinstance(exc, urllib.error.HTTPError) else None
        retryable = status_code is None or status_code >= 500
        if retryable:
            # A timeout or gateway error may have landed server-side; mark the
            # buffered copy so the drain verifies before re-sending —
            # /remember/entry has no idempotency, and a blind replay of a
            # committed write duplicates the trace into the next improve.
            append_warmup_entry(dataset, session_id, entry, ambiguous=write_outcome_ambiguous(exc))
            bump_save_counter(session_id, "trace", buffered=True)
            hook_log(
                "trace_buffered_after_error",
                {"tool": tool_name, "status": status_code, "error": str(exc)[:200]},
            )
            notify(f"trace store failed, buffered for replay ({exc})")
        else:
            if status_code == 402:
                record_payment_required("save")
            hook_log(
                "trace_store_error",
                {
                    "tool": tool_name,
                    "error": str(exc)[:200],
                    "status": status_code,
                    "buffered": False,
                },
            )
            notify(f"trace store failed ({exc})")
        return

    if result:
        clear_payment_required()
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
        bump_save_counter(session_id, "trace")

        touch_activity()
        count, should_improve = bump_turn_counter(session_id)
        if should_improve:
            await _fire_improve_background(dataset, session_id, reason=f"turn_{count}")
    else:
        hook_log("trace_store_noresult", {"tool": tool_name})


async def _store_assistant_stop(payload: dict) -> None:
    """Write a Stop-hook payload (final assistant message) as a QAEntry."""
    from _capture_policy import capture_enabled, redact

    if not capture_enabled():
        return
    payload = redact(payload)
    msg = str(payload.get("assistant_message") or payload.get("last_assistant_message") or "")
    if not msg or msg == "null":
        return

    msg = _truncate_str(msg, _MAX_ASSISTANT_BYTES)

    session_id, dataset, _user_id = _load_session()
    if not session_id:
        hook_log("no_session_id", {"event": "stop"})
        return

    runtime = resolve_runtime_mode()
    hook_log(
        "mode_decision",
        {
            "hook": "store-to-session:stop",
            "mode": runtime["mode"],
            "base_url": runtime.get("base_url", ""),
            "url_source": runtime.get("url_source", ""),
            "key_source": runtime.get("key_source", ""),
            "api_key_present": runtime.get("api_key_present", False),
        },
    )
    pending = pop_pending_prompt(session_id, turn_id=str(payload.get("turn_id") or ""))

    # Codex intentionally differs from Claude here: store one paired
    # prompt/answer row so Cognee's filesystem session cache does not get
    # separate question-only and answer-only QA entries for the same turn.
    entry = {
        "type": "qa",
        "question": redact(pending.get("prompt", "")),
        "answer": msg,
        "context": redact(pending.get("context", "")),
    }

    try:
        from _evaluation_capture import enqueue as enqueue_evaluation, launch_delivery

        enqueue_evaluation(
            entry["question"],
            entry["answer"],
            model=str(payload.get("model") or "unknown"),
            event_id=str(payload.get("turn_id") or pending.get("turn_id") or ""),
        )
        launch_delivery()
    except Exception as exc:
        hook_log("evaluation_capture_error", {"error": type(exc).__name__})

    if cloud_enabled(runtime):
        from _cloud_worker import enqueue, request_improve

        enqueue(dataset, session_id, entry, "answer")
        request_improve(dataset, session_id, "stop")
        bump_save_counter(session_id, "answer", buffered=True)
        return

    if not server_usable(runtime.get("base_url", "")):
        # Server unreachable (stale marker AND a failed probe): buffer the
        # structured entry for a later /remember/entry replay (improve bridges
        # only what the server session cache holds).
        append_warmup_entry(dataset, session_id, entry)
        bump_save_counter(session_id, "answer", buffered=True)
        hook_log("store_buffered_warming", {"hook": "stop"})
        return

    try:
        result = remember_entry_via_http(dataset, session_id, entry)
    except Exception as exc:
        # A write that FAILED must still be buffered, or the turn is simply lost.
        # The `server_usable()` guard above only catches an outage the plugin
        # already knows about: the ready marker has a 30s TTL, so a server that
        # dies inside that window leaves server_usable() returning True, the write
        # is attempted for real, and this is where it lands. Logging alone here is
        # what the warmup spillway exists to prevent.
        #
        # Not every failure is worth replaying, though. The drain stops at the
        # first entry it cannot send and only trims what it drained, so an entry
        # that can never succeed would sit at the head of the queue and block
        # everything behind it forever. A 4xx is exactly that: the same bytes will
        # be rejected the same way next time. Transport failures and 5xx are
        # retryable, so those are buffered and anything else is dropped loudly.
        status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
        retryable = status is None or status >= 500
        if retryable:
            # Ambiguous outcomes (timeout / gateway error after the request
            # went out) are verified against the server before replay — see
            # write_outcome_ambiguous.
            append_warmup_entry(dataset, session_id, entry, ambiguous=write_outcome_ambiguous(exc))
            bump_save_counter(session_id, "answer", buffered=True)
            hook_log(
                "store_buffered_after_error",
                {"hook": "stop", "status": status, "error": str(exc)[:200]},
            )
            notify(f"stop store failed, buffered for replay ({exc})")
        else:
            if status == 402:
                record_payment_required("save")
            hook_log(
                "stop_store_error",
                {"error": str(exc)[:200], "status": status, "buffered": False},
            )
            notify(f"stop store failed ({exc})")
        return

    if result:
        clear_payment_required()
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
            await _fire_improve_background(dataset, session_id, reason=f"turn_{count}")


def _maybe_reingest_code_repo(payload: dict) -> None:
    """Freshness pass for indexed code repos (runs on the Stop hook only).

    When this turn's cwd sits inside a repo indexed via cognee-index-repo.sh
    and the working tree's git fingerprint changed since the last index, the
    repo is re-submitted (background) so the code graph reflects the turn's
    edits by the next prompt. The fingerprint is only a cheap client-side
    gate — the server re-hashes every covered file anyway, so a false
    positive costs one skipped submission. Repos indexed by git URL are NOT
    re-submitted here: the server's clone only sees pushed commits, so local
    edits cannot reach it. Never raises: this must not disturb the hook.
    """
    try:
        from _code_graph import reingest_if_changed

        cwd = str(payload.get("cwd") or "") or os.getcwd()
        runtime = resolve_runtime_mode()
        service_url = runtime.get("base_url", "")
        if not service_url:
            return
        if not server_usable(service_url):
            # Down server: skip quietly. The stale fingerprint is kept, so the
            # next Stop retries once the server is back.
            hook_log("code_reingest_skipped_server", {"base_url": service_url})
            return
        outcome = reingest_if_changed(cwd, service_url, os.environ.get("COGNEE_API_KEY", ""))
        if not outcome:
            return
        if outcome.get("changed") and outcome.get("submitted"):
            hook_log("code_reingest_submitted", outcome)
            notify(f"code graph re-index submitted ({outcome.get('dataset', '')})")
        elif outcome.get("changed"):
            hook_log("code_reingest_failed", outcome)
        else:
            hook_log("code_reingest_unchanged", {"repo_root": outcome.get("repo_root", "")})
    except Exception as exc:
        hook_log("code_reingest_error", {"error": str(exc)[:200]})


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
                # End-of-turn code-graph freshness: independent of whether an
                # assistant message was stored (edits happen either way).
                if not cloud_enabled():
                    _maybe_reingest_code_repo(payload)
            else:
                asyncio.run(_store_tool_call(payload))
    except Exception as exc:
        hook_log("run_exception", {"stop": is_stop, "error": str(exc)[:200]})


if __name__ == "__main__":
    main()
