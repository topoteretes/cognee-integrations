#!/usr/bin/env python3
"""Background watcher daemon for Cursor sessions.

Launched detached from ``session-start.py`` (and respawned by the next prompt
if it is not running). Runs the update check and the LLM-key check once at
start, then refreshes shared agent memory every ``SHARED_REFRESH_SECONDS``
until stopped.

Stops cleanly on:
  * ``~/.cognee-plugin/cursor/watcher.stop`` sentinel file.
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

_PLUGIN_DIR = Path.home() / ".cognee-plugin" / "cursor"
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


def _run_update_check() -> None:
    """Fire the background, interval-guarded plugin update check (best-effort)."""
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        from _plugin_common import maybe_check_for_update  # type: ignore

        maybe_check_for_update()
    except Exception as exc:
        _log("update_check_error", error=str(exc)[:200])


def _check_llm_key(config: dict) -> None:
    """Provider-agnostic LLM-key validation for the status line (local mode).

    Runs in this BACKGROUND watcher — never a hot-path hook — because it imports
    cognee/litellm (heavy) and makes one tiny real LLM call. That real call is the
    only way to validate a key that works across ALL providers: litellm normalizes
    every provider's rejection into ``AuthenticationError``, so we don't hardcode
    any provider's endpoint/auth. (recall can't be used — the plugin passes
    only_context=True, which skips the LLM entirely.)

    ``max_tokens=1`` is a floor on cost, not a guarantee: providers differ on whether
    it caps reasoning tokens or only content, so a reasoning model may bill more than
    one token (or reject the request outright — see the classifier below). That is
    acceptable here because the call is infrequent, off the hot path, bounded by a
    15s timeout, and its response is discarded unread.

    Writes the shared llm-state marker: ``ok`` / ``auth_failed`` / ``not_set``.
    Local mode only (LLM_API_KEY is unused against a remote server). Throttled via
    the marker's ``checked_at``. Honors COGNEE_LLM_KEY_CHECK=off.
    """
    try:
        if os.environ.get("COGNEE_LLM_KEY_CHECK", "").strip().lower() in (
            "0",
            "false",
            "no",
            "off",
        ):
            return
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

        # Throttle against OUR OWN last verdict only. The marker is machine-wide, so
        # honouring another session's timestamp would let a keyless launch's verdict
        # stand in for ours and leave this session permanently unvalidated.
        interval = float(os.environ.get("COGNEE_LLM_CHECK_INTERVAL", "") or 300.0)
        prior = read_llm_state()
        if str(prior.get("session_key") or "") == get_session_key() and (
            time.time() - float(prior.get("checked_at", 0) or 0) < interval
        ):
            return

        from cognee.infrastructure.llm.config import get_llm_config

        cfg = get_llm_config()
        key = str(getattr(cfg, "llm_api_key", "") or "").strip()
        if not key:
            # Logged, not silent: this write is what puts ✕ (incorrect_llm_api_key) on the bar,
            # and tracking down an unexplained one cost real time.
            write_llm_state("not_set")
            _log("llm_key_not_set")
            return

        import litellm

        try:
            litellm.completion(
                model=cfg.llm_model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
                api_key=cfg.llm_api_key,
                api_base=getattr(cfg, "llm_endpoint", None) or None,
                custom_llm_provider=(getattr(cfg, "llm_provider", None) or None),
                timeout=15,
            )
            write_llm_state("ok")
            _log("llm_key_ok")
        except Exception as exc:
            # Classify by the provider's HTTP status, NOT by "did a completion
            # succeed": `max_tokens=1` legitimately 400s on reasoning models (the
            # single token goes to reasoning, leaving no room for content), so
            # demanding a clean 200 leaves a good key permanently unverified and an
            # earlier "not_set" unclearable. Providers authenticate BEFORE they
            # validate anything else, so any status other than 401/403 already
            # proves the key was accepted.
            status = getattr(exc, "status_code", None)
            try:
                status = int(status) if status is not None else None
            except (TypeError, ValueError):
                status = None
            if exc.__class__.__name__ == "AuthenticationError" or status in (401, 403):
                write_llm_state("auth_failed", detail=str(exc)[:200])
                _log("llm_key_auth_failed")
            elif status is not None:
                # Reached the provider and got a non-auth rejection (400 output
                # limit, 404 unknown model, 429, 5xx): the key itself works.
                write_llm_state("ok")
                _log("llm_key_ok", status=status)
            else:
                # No HTTP status → local/transport failure (timeout, DNS, bad
                # api_base): not a key verdict either way, so leave the marker
                # untouched rather than falsely accuse or falsely clear.
                _log("llm_key_check_inconclusive", error=str(exc)[:200])
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
    # Runs once per watcher launch (≈ once per session); the check itself is
    # internally rate-limited to ≤ once per COGNEE_UPDATE_CHECK_INTERVAL.
    _run_update_check()
    # Validate the LLM key once at session start (background, provider-agnostic).
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
