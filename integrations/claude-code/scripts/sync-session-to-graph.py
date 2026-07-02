#!/usr/bin/env python3
"""Bridge session cache entries into the permanent knowledge graph on session end.

Runs the integration's explicit session bridge:
  1. Persist session Q&A/trace cache into the permanent graph
  2. Sync graph knowledge back into the session cache for recall

Configuration:
    Resolves session identity from Cognee endpoints via API auth.
"""

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path

# Add scripts dir to path for config/_plugin_common imports
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    get_session_key,
    hook_log,
    http_api_ready,
    load_resolved,
    resolve_session_key_from_payload,
    resolve_user,
    resolved_http_endpoint_auth,
    run_session_improve,
    set_session_key,
    sync_lock,
    unregister_agent_via_http,
)
from config import (
    ensure_cognee_ready,
    ensure_dataset_ready,
    get_dataset,
    get_session_id,
    improve_session_local,
    load_config,
)

_STATE_DIR = Path.home() / ".cognee-plugin" / "claude-code"
_WATCHER_PID = _STATE_DIR / "watcher.pid"
_WATCHER_STOP = _STATE_DIR / "watcher.stop"
_DETACHED_ARG = "--detached-final"
_SESSION_END_ARG = "--session-end"
_FINAL_SYNC_ONCE_DIR = _STATE_DIR / "final-sync-once"
_FINAL_SYNC_ONCE_TTL_SECONDS = 3600
_DETACHED_RETRIES_DEFAULT = 3
_DETACHED_RETRY_DELAY_DEFAULT = 10.0
_SESSION_END_START_DELAY_DEFAULT = 2.0


def _stop_idle_watcher() -> None:
    """Signal the idle watcher to exit and drop its pidfile.

    Uses both a sentinel file (safe, polled by the watcher) and a
    SIGTERM (fast). Either path is sufficient; both together handle
    the SIGTERM-blocked-during-improve edge case.
    """
    try:
        _WATCHER_STOP.parent.mkdir(parents=True, exist_ok=True)
        _WATCHER_STOP.write_text("stop", encoding="utf-8")
    except Exception as exc:
        hook_log("watcher_stop_write_failed", {"error": str(exc)[:200]})
    if _WATCHER_PID.exists():
        try:
            pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            hook_log("watcher_sigterm_failed", {"error": str(exc)[:200]})


def _spawn_detached_sync(cwd: str = "") -> bool:
    """Run the expensive sync outside a short hook window.

    The detached worker gets no stdin payload, so it can't recover the project
    ``cwd`` on its own. Propagate it (and the picker-resolved dataset) via env so
    the final session-end flush targets the dataset the project picked, not the
    global default — otherwise a ``.cognee/session-config.json`` dataset would be
    honored all session and then silently dropped at the final sync.
    """
    try:
        env = os.environ.copy()
        env.setdefault("COGNEE_SYNC_START_DELAY", str(_SESSION_END_START_DELAY_DEFAULT))
        env["COGNEE_UNREGISTER_ON_FINISH"] = "1"
        if cwd:
            env["CLAUDE_CWD"] = cwd
        # Resolve the active (picker-aware) dataset now, while cwd is known, and
        # pin it for the detached worker. setdefault so an explicit
        # COGNEE_SYNC_DATASET from an upstream spawner still wins.
        try:
            picked_dataset = str(get_dataset(load_config(cwd)) or "").strip()
            if picked_dataset:
                env.setdefault("COGNEE_SYNC_DATASET", picked_dataset)
        except Exception as exc:
            hook_log("sync_detach_dataset_resolve_failed", {"error": str(exc)[:200]})
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), _DETACHED_ARG],
            cwd=os.getcwd(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception as exc:
        hook_log("sync_detach_failed", {"error": str(exc)[:300]})
        return False


def _final_sync_identity() -> tuple[str, str]:
    """Return a stable per-session token for detached final sync dedupe."""
    session_key = str(os.environ.get("COGNEE_SESSION_KEY", "") or "").strip()
    if session_key:
        return session_key, "COGNEE_SESSION_KEY"
    session_id = str(os.environ.get("COGNEE_SYNC_SESSION_ID", "") or "").strip()
    if session_id:
        return session_id, "COGNEE_SYNC_SESSION_ID"
    return "", "missing"


def _claim_final_sync_once() -> bool:
    """Allow exactly one detached final sync worker per session."""
    _prune_final_sync_markers()

    token, source = _final_sync_identity()
    if not token:
        # No stable identity available; do not risk skipping final sync.
        hook_log("final_sync_once_no_token", {"source": source})
        return True

    digest = hashlib.sha1(token.encode("utf-8")).hexdigest()
    marker = _FINAL_SYNC_ONCE_DIR / f"{digest}.done"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
        hook_log("final_sync_once_claimed", {"source": source, "marker": str(marker)})
        return True
    except FileExistsError:
        hook_log("final_sync_once_already_claimed", {"source": source, "marker": str(marker)})
        return False
    except Exception as exc:
        # On marker failure, prefer proceeding to avoid data loss.
        hook_log("final_sync_once_claim_failed", {"source": source, "error": str(exc)[:200]})
        return True


def _prune_final_sync_markers() -> None:
    """Delete stale detached-sync dedupe markers older than configured TTL."""
    try:
        if not _FINAL_SYNC_ONCE_DIR.exists():
            return
        now = time.time()
        removed = 0
        for path in _FINAL_SYNC_ONCE_DIR.glob("*.done"):
            try:
                age = now - path.stat().st_mtime
                if age > _FINAL_SYNC_ONCE_TTL_SECONDS:
                    path.unlink()
                    removed += 1
            except FileNotFoundError:
                continue
            except Exception:
                continue
        if removed:
            hook_log(
                "final_sync_once_pruned",
                {"removed": removed, "ttl_seconds": _FINAL_SYNC_ONCE_TTL_SECONDS},
            )
    except Exception as exc:
        hook_log("final_sync_once_prune_failed", {"error": str(exc)[:200]})


def _is_session_end_payload(payload_raw: str) -> bool:
    """Return True only for an actual SessionEnd hook payload."""
    if not payload_raw.strip():
        return False
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        return False

    def _contains_session_end(value) -> bool:
        if isinstance(value, dict):
            return any(_contains_session_end(item) for item in value.values())
        if isinstance(value, list):
            return any(_contains_session_end(item) for item in value)
        if isinstance(value, str):
            return value == "SessionEnd" or value.endswith(".SessionEnd")
        return False

    event = (
        payload.get("hook_event_name")
        or payload.get("hookEventName")
        or payload.get("event")
        or payload.get("hook")
    )
    return event == "SessionEnd" or _contains_session_end(payload)


def _load_resolved(cwd: str = "") -> tuple:
    """
    Load session ID, dataset, user ID,
    agent session name, registration marker, and API key marker.
    """
    session_key = set_session_key(get_session_key())
    env_session_id = str(os.environ.get("COGNEE_SYNC_SESSION_ID", "") or "").strip()
    env_dataset = str(os.environ.get("COGNEE_SYNC_DATASET", "") or "").strip()
    env_agent_session_name = str(os.environ.get("COGNEE_AGENT_SESSION_NAME", "") or "").strip()
    env_api_key = str(os.environ.get("COGNEE_API_KEY", "") or "").strip()
    env_service_url = str(os.environ.get("COGNEE_BASE_URL", "") or "").strip()
    resolved_service_url, resolved_api_key = resolved_http_endpoint_auth()
    env_api_key = env_api_key or resolved_api_key
    env_service_url = env_service_url or resolved_service_url

    if not session_key:
        hook_log("sync_missing_session_key")
    data = load_resolved(session_key=session_key)
    if data:
        service_url = env_service_url or str(data.get("base_url", "") or "").strip()
        if service_url:
            os.environ["COGNEE_BASE_URL"] = service_url
        if data.get("user_id"):
            os.environ["COGNEE_USER_ID"] = str(data.get("user_id"))
        return (
            env_session_id or data.get("session_id", ""),
            # load_resolved() carries no dataset key, so without the env
            # override (only the exit watcher sets it) this must fall back to
            # config, or the SessionEnd worker syncs with an empty dataset.
            env_dataset or data.get("dataset", "") or get_dataset(load_config()),
            data.get("user_id", ""),
            env_agent_session_name or data.get("agent_session_name", ""),
            bool(data.get("registered", False)),
            bool(env_api_key or data.get("api_key", "")),
            session_key,
        )

    config = load_config(cwd)
    fallback_session_id = get_session_id(config, cwd)
    fallback_agent_session_name = session_key or ""
    if env_service_url:
        os.environ["COGNEE_BASE_URL"] = env_service_url
    return (
        env_session_id or fallback_session_id,
        env_dataset or get_dataset(config),
        "",
        env_agent_session_name or fallback_agent_session_name,
        False,
        bool(env_api_key),
        session_key,
    )


async def _sync(
    stop_watcher: bool, unregister_on_finish: bool = False, strict: bool = False, cwd: str = ""
):
    session_id, dataset, user_id, agent_session_name, was_registered, has_api_key, session_key = (
        _load_resolved(cwd)
    )
    target_sessions = [session_id] if session_id else []
    hook_log(
        "sync_start",
        {
            "session": session_id,
            "targets": target_sessions,
            "dataset": dataset,
            "user_id": user_id,
            "stop_watcher": stop_watcher,
        },
    )

    try:
        if stop_watcher:
            _stop_idle_watcher()
            hook_log("sync_stopped_watcher", {"session": session_id, "dataset": dataset})

        config = load_config(cwd)
        api_mode = http_api_ready()
        lock = nullcontext(True) if api_mode else sync_lock("sync-session-to-graph")
        with lock as acquired:
            if not acquired:
                hook_log("sync_skipped_lock_busy", {"session": session_id, "dataset": dataset})
                print("cognee-sync: skipped, another sync is running", file=sys.stderr)
                return

            if not target_sessions:
                hook_log("sync_no_target_sessions", {"dataset": dataset})
                return

            incomplete: list[str] = []
            if api_mode:
                for sid in target_sessions:
                    wrote = run_session_improve(dataset, sid)
                    if not wrote:
                        incomplete.append(sid)
                    hook_log(
                        "sync_bridge_done",
                        {
                            "session": sid,
                            "dataset": dataset,
                            "via": "http_improve",
                            "wrote": wrote,
                        },
                    )
                    print(
                        f"cognee-sync: dataset={dataset} session={sid} "
                        f"via=http_improve wrote={wrote}",
                        file=sys.stderr,
                    )
                if strict and incomplete:
                    # The detached final worker retries on exceptions only. This
                    # is the session's LAST sync — an incomplete one (failed
                    # improve or undelivered warmup entries) must re-drive the
                    # whole drain+improve, not silently report success.
                    raise RuntimeError(
                        f"final session sync incomplete for: {', '.join(incomplete)}"
                    )
                return

            await ensure_cognee_ready(config)
            user = await resolve_user(user_id)
            await ensure_dataset_ready(dataset, user)
            for sid in target_sessions:
                result = await improve_session_local(dataset, sid, user)
                if not result.get("ok"):
                    incomplete.append(sid)
                hook_log(
                    "sync_bridge_done",
                    {
                        "session": sid,
                        "dataset": dataset,
                        "user_id": str(getattr(user, "id", "")),
                        "via": "local_improve",
                        "ok": bool(result.get("ok")),
                    },
                )
                print(
                    f"cognee-sync: dataset={dataset} session={sid} "
                    f"via=local_improve ok={result.get('ok')}",
                    file=sys.stderr,
                )
            if strict and incomplete:
                raise RuntimeError(f"final session sync incomplete for: {', '.join(incomplete)}")
    finally:
        if unregister_on_finish:
            if not (was_registered or has_api_key):
                hook_log(
                    "agent_unregister_skipped_no_auth",
                    {"session": session_id, "dataset": dataset},
                )
            else:
                unregister_name = str(agent_session_name or session_key or "").strip()
                if not unregister_name:
                    hook_log(
                        "agent_unregister_skipped_no_session_name",
                        {"session": session_id, "dataset": dataset},
                    )
                    return
                ok, active = unregister_agent_via_http(agent_session_name=unregister_name)
                hook_log(
                    "agent_unregister_result",
                    {
                        "session": session_id,
                        "dataset": dataset,
                        "agent_session_name": unregister_name,
                        "ok": ok,
                        "active_agents": active,
                        "cached_registered": was_registered,
                    },
                )


def main():
    detached_final = _DETACHED_ARG in sys.argv
    forced_session_end = _SESSION_END_ARG in sys.argv
    payload_raw = "" if detached_final else sys.stdin.read()
    payload = {}
    if not detached_final and payload_raw.strip():
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            payload = {}
        session_key_candidate, session_key_source = resolve_session_key_from_payload(payload)
        if session_key_candidate:
            set_session_key(session_key_candidate)
        hook_log("sync_session_key", {"source": session_key_source, "value": session_key_candidate})

    cwd = str(payload.get("cwd") or "")
    is_session_end = forced_session_end or _is_session_end_payload(payload_raw)
    hook_log(
        "sync_payload",
        {
            "is_session_end": is_session_end,
            "detached_final": detached_final,
            "forced_session_end": forced_session_end,
            "payload_preview": payload_raw[:200],
        },
    )

    if detached_final:
        delay_raw = os.environ.get("COGNEE_SYNC_START_DELAY", "")
        try:
            delay = float(delay_raw) if delay_raw else 0.0
        except ValueError:
            delay = 0.0
        if delay > 0:
            hook_log("sync_start_delayed", {"seconds": delay})
            time.sleep(delay)
        if not _claim_final_sync_once():
            hook_log("sync_detached_skipped_duplicate")
            return

    unregister_on_finish = detached_final and os.environ.get(
        "COGNEE_UNREGISTER_ON_FINISH", ""
    ).lower() in ("1", "true", "yes")

    # Only a true SessionEnd should stop the watcher. Manual syncs and
    # slash-command invocations happen mid-session, and killing the watcher
    # there prevents later idle persistence.
    if is_session_end:
        _stop_idle_watcher()
        spawned = _spawn_detached_sync(cwd)
        hook_log("sync_deferred_to_shutdown_worker", {"spawned": spawned})
        return

    attempts = 1
    retry_delay = 0.0
    if detached_final:
        attempts = int(os.environ.get("COGNEE_SYNC_RETRIES", str(_DETACHED_RETRIES_DEFAULT)))
        retry_delay = float(
            os.environ.get("COGNEE_SYNC_RETRY_DELAY", str(_DETACHED_RETRY_DELAY_DEFAULT))
        )

    for attempt in range(1, max(1, attempts) + 1):
        try:
            asyncio.run(
                _sync(
                    stop_watcher=False,
                    unregister_on_finish=unregister_on_finish,
                    # The detached-final run is the session's last sync: an
                    # incomplete result raises so this loop retries it.
                    strict=detached_final,
                    cwd=cwd,
                )
            )
            return
        except Exception as exc:
            # Non-fatal: session sync failure should not crash Codex.
            hook_log(
                "sync_failed",
                {"attempt": attempt, "attempts": attempts, "error": str(exc)[:300]},
            )
            print(f"cognee-sync: failed ({exc})", file=sys.stderr)
            if attempt < attempts:
                hook_log("sync_retry_scheduled", {"attempt": attempt + 1, "delay": retry_delay})
                time.sleep(retry_delay)


if __name__ == "__main__":
    main()
