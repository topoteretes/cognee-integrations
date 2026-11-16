#!/usr/bin/env python3
"""Background watcher daemon.

Launched detached from ``session-start.py`` (and respawned by the next prompt
if it is not running). Runs the update check once at start, then refreshes shared agent memory every
``SHARED_REFRESH_SECONDS`` and renews the LLM-configured check every
``LLM_CHECK_SECONDS`` until stopped.

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
# How often the "local server has no LLM" verdict is renewed. Well inside the
# status line's 30-minute TTL for it, so the mark holds for the whole session.
LLM_CHECK_SECONDS = 300.0

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


def _llm_options() -> tuple[bool, str] | None:
    """``(LLM key option set, LLM provider)`` for this launch, or None when unknown.

    Session start had the plugin options and recorded both on the launch
    record. This process may have none: the watcher a Bash-run skill restarts
    (a dataset switch) is given no options, and reading "no key" from its own
    environment put a false mark on the bar of a user whose key is set. So the
    record decides, and this process's own key option only ever says "set".
    """
    from _env_file import option
    from _plugin_common import _read_map_record, get_session_key

    provider = os.environ.get("LLM_PROVIDER", "").strip()
    facts = _read_map_record(get_session_key()).get("launch_facts")
    recorded = isinstance(facts, dict) and "llm_key_option_set" in facts
    if recorded:
        provider = str(facts.get("llm_provider") or "").strip() or provider
    if option("llm_api_key"):
        return True, provider
    if recorded:
        return bool(facts["llm_key_option_set"]), provider
    return None


def _check_llm_key(config: dict) -> None:
    """Say on the status line when the local server has no LLM (local mode).

    The local server's LLM comes from the plugin options alone, so "no usable
    LLM option" is known without asking anyone (``_llm_options``): it writes
    ``not_set`` to the llm-state marker, and withdraws the verdict otherwise.
    Run at start and again every ``LLM_CHECK_SECONDS``, because the status line
    drops a verdict older than its TTL. Whether a configured key works is not
    checked (SDK-1411): the plugin imports no cognee or litellm code and calls
    no provider, so a bad key shows up only as failures in the server's logs.
    """
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        from _plugin_common import (
            clear_llm_state,
            live_server_record,
            llm_configured,
            llm_key_owner,
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
        options = _llm_options()
        if options is None:
            # Nothing says what this launch was given: no verdict either way.
            _log("llm_key_check_skipped", reason="no_launch_facts")
            return
        if llm_configured(*options):
            clear_llm_state()
            _log("llm_key_check_skipped", reason="configured")
            return
        # A server an older version started on the Claude observer (removed in
        # 1.8.4) keeps that LLM until it restarts.
        try:
            import urllib.parse

            port = urllib.parse.urlparse(base_url or "http://localhost:8011").port or 80
            on_observer = live_server_record(port).get("llm_observer") is True
        except Exception:
            on_observer = False
        if on_observer:
            clear_llm_state()
            _log("llm_key_check_skipped", reason="server_on_observer")
            return
        write_llm_state("not_set")
        _log("llm_key_not_set")
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
    # Whether the local server has an LLM: now, and again while the session runs.
    _check_llm_key(config)
    next_llm_check = time.time() + LLM_CHECK_SECONDS
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
        if now >= next_llm_check:
            next_llm_check = now + LLM_CHECK_SECONDS
            _check_llm_key(config)
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
