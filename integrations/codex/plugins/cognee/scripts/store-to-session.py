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
import urllib.error

# Add scripts dir to path for helper imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    append_warmup_entry,
    bump_save_counter,
    bump_turn_counter,
    get_session_key,
    hook_log,
    http_api_ready,
    improve_throttle_reason,
    load_resolved,
    notify,
    pop_pending_prompt,
    quiet_hook_output,
    remember_entry_via_http,
    resolve_runtime_mode,
    resolve_session_key_from_payload,
    resolve_user,
    run_session_improve,
    server_usable,
    set_session_key,
    touch_activity,
    write_outcome_ambiguous,
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

    The server bridges the session itself from its session cache (improve);
    see run_session_improve. Shares the cooldown / no-new-entries gate with the
    idle watcher; the session-end sync ignores it and covers whatever a skip
    here leaves behind.
    """
    throttled = improve_throttle_reason(session_id)
    if throttled:
        hook_log(
            "auto_improve_throttled",
            {"reason": reason, "session": session_id, "why": throttled},
        )
        return
    try:
        if http_api_ready():
            wrote = run_session_improve(dataset, session_id, trigger="auto")
            hook_log(
                "auto_improve_fired",
                {"reason": reason, "session": session_id, "via": "http_improve", "wrote": wrote},
            )
            if wrote:
                notify(f"session improve submitted ({reason})")
            return

        await ensure_dataset_ready(dataset, user)
        result = await improve_session_local(dataset, session_id, user, trigger="auto")
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

    if not server_usable(runtime.get("base_url", "")):
        # Server unreachable (stale marker AND a failed probe — a stale marker
        # alone no longer buffers; this hook fires per tool call, so its probe
        # keeps the ready marker fresh through long turns, #298): don't block
        # the tool call and don't lose the trace. Buffer the structured entry
        # for a later /remember/entry replay (improve bridges only what the
        # server session cache holds).
        append_warmup_entry(dataset, session_id, entry)
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
            bump_save_counter(session_id, "trace")
            hook_log(
                "trace_buffered_after_error",
                {"tool": tool_name, "status": status_code, "error": str(exc)[:200]},
            )
            notify(f"trace store failed, buffered for replay ({exc})")
        else:
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

    if not server_usable(runtime.get("base_url", "")):
        # Server unreachable (stale marker AND a failed probe): buffer the
        # structured entry for a later /remember/entry replay (improve bridges
        # only what the server session cache holds).
        append_warmup_entry(dataset, session_id, entry)
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
            bump_save_counter(session_id, "answer")
            hook_log(
                "store_buffered_after_error",
                {"hook": "stop", "status": status, "error": str(exc)[:200]},
            )
            notify(f"stop store failed, buffered for replay ({exc})")
        else:
            hook_log(
                "stop_store_error",
                {"error": str(exc)[:200], "status": status, "buffered": False},
            )
            notify(f"stop store failed ({exc})")
        return

    if result:
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
                _maybe_reingest_code_repo(payload)
            else:
                asyncio.run(_store_tool_call(payload))
    except Exception as exc:
        hook_log("run_exception", {"stop": is_stop, "error": str(exc)[:200]})


if __name__ == "__main__":
    main()
