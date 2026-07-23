#!/usr/bin/env python3
"""Store the user's prompt until Codex Stop provides the assistant answer.

Runs async on the UserPromptSubmit hook so it doesn't block the
parallel context-lookup hook. Unlike the Claude integration, Codex keeps
the prompt pending and writes a single paired QAEntry on Stop.

Configuration:
    Resolves session state via Cognee HTTP endpoints.
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
    bump_save_counter,
    get_session_key,
    hook_log,
    load_resolved,
    notify,
    quiet_hook_output,
    remember_pending_prompt,
    resolve_runtime_mode,
    resolve_session_key_from_payload,
    resolve_user,
    server_ready_hint,
    set_session_key,
    touch_activity,
)
from _proc import pid_alive
from config import ensure_cognee_ready, get_dataset, get_session_id, load_config

MAX_TEXT = 4000
_STATE_DIR = Path.home() / ".cognee-plugin" / "claude-code"
_WATCHER_PID = _STATE_DIR / "watcher.pid"
_WATCHER_STOP = _STATE_DIR / "watcher.stop"
_WATCHER_SCRIPT = Path(__file__).with_name("idle-watcher.py")


def _load_session(cwd: str = "") -> tuple[str, str, str]:
    resolved = load_resolved()
    session_id = resolved.get("session_id", "")
    dataset = resolved.get("dataset", "")
    user_id = resolved.get("user_id", "")
    if not session_id or not dataset:
        config = load_config(cwd)
        session_id = session_id or get_session_id(config, cwd)
        dataset = dataset or get_dataset(config)
    return session_id, dataset, user_id


def _watcher_alive() -> bool:
    if not _WATCHER_PID.exists():
        return False
    try:
        pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
    except Exception as exc:
        hook_log("prompt_watcher_alive_check_failed", {"error": str(exc)[:200]})
        return False
    return pid_alive(pid)


def _ensure_idle_watcher(session_id: str, dataset: str, user_id: str, config: dict) -> None:
    """Start the idle watcher on a new prompt if the prior one exited after bridging."""
    if os.environ.get("COGNEE_IDLE_DISABLED", "").lower() in ("1", "true", "yes"):
        return
    if not session_id or _watcher_alive():
        return

    try:
        if _WATCHER_STOP.exists():
            _WATCHER_STOP.unlink()
    except Exception as exc:
        hook_log("prompt_watcher_stop_unlink_failed", {"error": str(exc)[:200]})

    bootstrap = {
        "session_id": session_id,
        "dataset": dataset,
        "user_id": user_id,
        "session_key": os.environ.get("COGNEE_SESSION_KEY", ""),
        "config": {
            "base_url": config.get("base_url", ""),
            "llm_model": config.get("llm_model", ""),
            "dataset": dataset,
        },
    }

    log_path = _STATE_DIR / "watcher.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")
    except Exception as exc:
        hook_log("prompt_watcher_log_open_failed", {"error": str(exc)[:200]})
        log_fh = subprocess.DEVNULL

    try:
        env = os.environ.copy()
        subprocess.Popen(
            [sys.executable, str(_WATCHER_SCRIPT), json.dumps(bootstrap)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        hook_log("idle_watcher_restarted", {"session": session_id, "dataset": dataset})
    except Exception as exc:
        hook_log("idle_watcher_restart_failed", {"error": str(exc)[:200]})


def _prompt_context(payload: dict) -> str:
    context = {
        "cwd": payload.get("cwd"),
        "model": payload.get("model"),
        "turn_id": payload.get("turn_id"),
        "transcript_path": payload.get("transcript_path"),
    }
    return json.dumps({k: v for k, v in context.items() if v}, default=str)


async def _store(prompt: str, payload: dict):
    cwd = str(payload.get("cwd") or "")
    session_id, dataset, user_id = _load_session(cwd)
    if not session_id:
        hook_log("no_session_id", {"event": "prompt"})
        return

    config = load_config(cwd)
    touch_activity()
    _ensure_idle_watcher(session_id, dataset, user_id, config)

    runtime = resolve_runtime_mode()
    hook_log(
        "mode_decision",
        {
            "hook": "store-user-prompt",
            "mode": runtime["mode"],
            "base_url": runtime.get("base_url", ""),
            "url_source": runtime.get("url_source", ""),
            "key_source": runtime.get("key_source", ""),
            "api_key_present": runtime.get("api_key_present", False),
        },
    )
    if runtime["mode"] == "local_sdk" and server_ready_hint(runtime.get("base_url", "")):
        # Keep Cognee initialization parity with Claude so fresh local
        # databases, identities, and datasets are ready before Stop writes.
        # Skipped while the server is still warming so this hook never blocks;
        # the prompt is still buffered below and flushed once the server is up.
        try:
            await ensure_cognee_ready(config)
            await resolve_user(user_id)
        except Exception as exc:
            hook_log("prompt_prepare_warning", {"error": str(exc)[:200]})

    remember_pending_prompt(
        session_id,
        prompt[:MAX_TEXT],
        turn_id=str(payload.get("turn_id") or ""),
        context=_prompt_context(payload),
    )
    hook_log("prompt_pending", {"chars": len(prompt), "turn_id": payload.get("turn_id")})
    notify(f"user prompt pending ({len(prompt)} chars)")
    bump_save_counter(session_id, "prompt")


def main():
    payload_raw = sys.stdin.read()
    if not payload_raw.strip():
        return

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        hook_log("invalid_payload_json", {"event": "prompt"})
        return

    session_key_candidate, session_key_source = resolve_session_key_from_payload(payload)
    if session_key_candidate:
        set_session_key(session_key_candidate)
    hook_log("prompt_session_key", {"source": session_key_source, "value": session_key_candidate})
    if not get_session_key():
        hook_log("prompt_missing_session_key")
        return

    prompt = payload.get("prompt", "")
    if not prompt or len(prompt) < 5:
        return

    try:
        with quiet_hook_output("store-user-prompt"):
            asyncio.run(_store(prompt, payload))
    except Exception as exc:
        hook_log("prompt_run_exception", {"error": str(exc)[:200]})


if __name__ == "__main__":
    main()
