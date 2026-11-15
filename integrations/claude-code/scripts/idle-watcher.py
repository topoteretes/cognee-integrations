#!/usr/bin/env python3
"""Background watcher daemon.

Launched detached from ``session-start.py`` (and respawned by the next prompt
if it is not running). Runs the update check and the observer LLM check once
at start, then refreshes shared agent memory every ``SHARED_REFRESH_SECONDS``
until stopped.

Stops cleanly on:
  * ``~/.cognee-plugin/watcher.stop`` sentinel file.
  * Receiving SIGTERM (from SessionEnd hook or manual kill).
  * The pidfile being overwritten by a newer watcher (restart case).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

from _logfiles import append_line as _append_log_line
from event_names import event_fields

# Loop tick: bounds how quickly a stop (sentinel, SIGTERM, replaced pidfile)
# is noticed and how late a due shared-memory refresh can run.
POLL_SECONDS = float(os.environ.get("COGNEE_IDLE_POLL", "10"))
# Shared agent memory: how often to re-resolve the active dataset's UUIDs and
# backfill the shared role's grants, so a dataset another plugin created shows
# up here within this interval rather than at the next session start.
SHARED_REFRESH_SECONDS = float(os.environ.get("COGNEE_SHARED_MEMORY_REFRESH", "60"))

_PLUGIN_DIR = Path.home() / ".cognee-plugin" / "claude-code"
_PIDFILE = _PLUGIN_DIR / "watcher.pid"
_STOPFILE = _PLUGIN_DIR / "watcher.stop"
_LOGFILE = _PLUGIN_DIR / "watcher.log"

# Script-local stop flag flipped by SIGTERM handler.
_should_stop = False


def _log(event: str, **detail) -> None:
    try:
        line = {
            "ts": time.time(),
            "pid": os.getpid(),
            "event": event,
            **event_fields(event, "idle-watcher"),
        }
        if detail:
            line["detail"] = detail
        _append_log_line(_LOGFILE, json.dumps(line, default=str))
    except Exception:
        pass


def _owns_pidfile() -> bool:
    """Return True if the pidfile still points at us."""
    try:
        return int(_PIDFILE.read_text(encoding="utf-8").strip()) == os.getpid()
    except Exception as exc:
        _log("pidfile_read_failed", error=str(exc)[:200])
        return False


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        global _should_stop
        _should_stop = True
        _log("signal_received", signum=signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


# Minimum seconds between two observer checks by the same session.
_LLM_CHECK_INTERVAL_SECONDS = 300.0


def _check_llm_key(config: dict) -> None:
    """Observer-only LLM check for the status line (local mode).

    The only LLM verdict the plugin forms is about the Claude observer: it asks
    the plugin's own loopback shim whether ``claude`` can answer. A user's own
    LLM key is not checked here (SDK-1411): the plugin imports no cognee or
    litellm code and calls no provider, so a bad key shows up only as failures
    in the server's logs.

    Writes the shared llm-state marker: ``ok`` / ``auth_failed`` (reason
    ``observer_needs_stored_login``). Without an active observer it withdraws
    this session's verdict. Throttled via the marker's ``checked_at``.
    """
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        from _plugin_common import (
            clear_llm_state,
            get_session_key,
            llm_key_owner,
            read_llm_state,
            service_url_is_local,
            write_llm_state,
        )

        base_url = str(config.get("base_url") or "")
        if base_url and not service_url_is_local(base_url):
            return  # cloud: the remote server owns its own LLM key
        # Loopback is not ownership: a server this plugin did not start reads its
        # key from its own environment, so ours says nothing about it (#371).
        owner = llm_key_owner(config)
        if owner:
            clear_llm_state()
            _log("llm_key_check_skipped", reason=owner)
            return

        from _observer import STORED_LOGIN_ONLY_MESSAGE, STORED_LOGIN_ONLY_REASON, observer_probe
        from _observer import is_active as _observer_active

        if not _observer_active():
            clear_llm_state()
            _log("llm_key_check_skipped", reason="observer_inactive")
            return

        # Throttle against OUR OWN last verdict only. The marker is machine-wide, so
        # honouring another session's timestamp would let a keyless launch's verdict
        # stand in for ours and leave this session permanently unvalidated.
        prior = read_llm_state()
        if str(prior.get("session_key") or "") == get_session_key() and (
            time.time() - float(prior.get("checked_at", 0) or 0) < _LLM_CHECK_INTERVAL_SECONDS
        ):
            return

        # The "key" is Claude Code's own stored login, and the shim knows whether
        # `claude` can answer.
        verdict = observer_probe(timeout=75.0)
        auth = verdict.get("auth")
        if auth == "ok":
            write_llm_state("ok")
            _log("llm_key_ok", source="observer")
        elif auth == "failed":
            write_llm_state(
                "auth_failed",
                detail=STORED_LOGIN_ONLY_MESSAGE,
                reason=STORED_LOGIN_ONLY_REASON,
            )
            _log(
                "llm_key_auth_failed",
                source="observer",
                detail=str(verdict.get("detail") or "")[:200],
            )
        else:
            # The shim is down or claude failed for a non-auth reason: no verdict
            # either way.
            _log(
                "llm_key_check_inconclusive",
                source="observer",
                status=verdict.get("status"),
                error=str(verdict.get("detail") or "")[:200],
            )
    except Exception as exc:
        _log("llm_key_check_error", error=str(exc)[:200])


async def _main_loop(session_id: str, dataset: str, config: dict) -> None:
    _log(
        "started",
        session=session_id,
        dataset=dataset,
        user_id=config.get("user_id", ""),
        poll=POLL_SECONDS,
    )
    # Check the Claude observer once at session start (background).
    _check_llm_key(config)
    exit_reason = "loop_complete"
    # First shared-memory refresh one interval in: SessionStart just resolved
    # everything, so an immediate re-resolve would only repeat its calls.
    next_shared_refresh = time.time() + SHARED_REFRESH_SECONDS

    def _refresh_shared_memory() -> None:
        try:
            from _plugin_common import refresh_shared_memory

            if refresh_shared_memory():
                _log("shared_memory_refreshed", session=session_id)
        except Exception as exc:
            _log("shared_memory_refresh_failed", error=str(exc)[:200])

    while not _should_stop:
        if _STOPFILE.exists():
            _log("stop_sentinel_seen")
            exit_reason = "stop_sentinel"
            break
        if not _owns_pidfile():
            _log("pidfile_replaced")
            exit_reason = "pidfile_replaced"
            break

        now = time.time()
        if SHARED_REFRESH_SECONDS > 0 and now >= next_shared_refresh:
            next_shared_refresh = now + SHARED_REFRESH_SECONDS
            _refresh_shared_memory()
        await asyncio.sleep(POLL_SECONDS)

    if _should_stop:
        exit_reason = "signal"

    _log("exiting", reason=exit_reason)
    try:
        if _owns_pidfile():
            _PIDFILE.unlink()
    except Exception as exc:
        _log("pidfile_unlink_failed", error=str(exc)[:200])


def main():
    from _cloud_worker import enabled, ensure_worker

    if enabled():
        ensure_worker()
        return
    _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)

    # Config passed as a single JSON arg to avoid shell-quoting hazards.
    if len(sys.argv) < 2:
        _log("fatal_missing_args")
        sys.exit(1)
    try:
        bootstrap = json.loads(sys.argv[1])
    except Exception as exc:
        _log("fatal_bad_args", error=str(exc)[:200])
        sys.exit(1)

    session_id = bootstrap.get("session_id", "")
    dataset = bootstrap.get("dataset", "agent_sessions")
    user_id = bootstrap.get("user_id", "")
    session_key = str(bootstrap.get("session_key", "") or "").strip()
    try:
        from config import load_config  # type: ignore

        config = load_config()
        config.update({k: v for k, v in bootstrap.get("config", {}).items() if v})
    except Exception as exc:
        _log("config_load_failed", error=str(exc)[:200])
        config = bootstrap.get("config", {})
    if not session_id:
        _log("fatal_no_session_id")
        sys.exit(1)
    if user_id:
        config["user_id"] = user_id
        os.environ["COGNEE_USER_ID"] = user_id
    if session_key:
        config["session_key"] = session_key
        os.environ["COGNEE_SESSION_KEY"] = session_key

    try:
        _PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as exc:
        _log("pidfile_write_failed", error=str(exc)[:200])
        sys.exit(1)

    # Make sure a stale stop sentinel from a prior run doesn't kill us
    # the moment we start.
    try:
        if _STOPFILE.exists():
            _STOPFILE.unlink()
    except Exception as exc:
        _log("stopfile_unlink_failed", error=str(exc)[:200])

    _install_signal_handlers()

    try:
        asyncio.run(_main_loop(session_id, dataset, config))
    except Exception as exc:
        _log("fatal_loop_error", error=str(exc)[:300])


if __name__ == "__main__":
    main()
