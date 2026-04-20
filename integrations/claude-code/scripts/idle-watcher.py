#!/usr/bin/env python3
"""Idle watcher daemon — triggers ``cognee.improve()`` on quiet sessions.

Launched detached from ``session-start.py``. Polls
``~/.cognee-plugin/activity.ts`` every ``POLL_SECONDS``. When the last
activity is older than ``IDLE_SECONDS`` and we haven't improved since
that point, fires ``cognee.improve(session_ids=[session_id])``.

Stops cleanly on:
  * ``~/.cognee-plugin/watcher.stop`` sentinel file.
  * Receiving SIGTERM (from SessionEnd hook or manual kill).
  * The pidfile being overwritten by a newer watcher (restart case).

Survives SessionEnd / Claude crashes better than the SessionEnd hook
does — that hook won't run if Claude was killed hard.
"""

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

# Tunable via env. Defaults chosen to avoid thrashing the LLM: 60s idle
# threshold means you have to actively pause a full minute, and the 20s
# improve cooldown prevents back-to-back runs when activity is sporadic.
POLL_SECONDS = float(os.environ.get("COGNEE_IDLE_POLL", "10"))
IDLE_SECONDS = float(os.environ.get("COGNEE_IDLE_THRESHOLD", "60"))
IMPROVE_COOLDOWN = float(os.environ.get("COGNEE_IMPROVE_COOLDOWN", "120"))

_PLUGIN_DIR = Path.home() / ".cognee-plugin"
_ACTIVITY = _PLUGIN_DIR / "activity.ts"
_PIDFILE = _PLUGIN_DIR / "watcher.pid"
_STOPFILE = _PLUGIN_DIR / "watcher.stop"
_LOGFILE = _PLUGIN_DIR / "watcher.log"

# Script-local stop flag flipped by SIGTERM handler.
_should_stop = False


def _log(event: str, **detail) -> None:
    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        line = {"ts": time.time(), "pid": os.getpid(), "event": event}
        if detail:
            line["detail"] = detail
        with _LOGFILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
    except Exception:
        pass


def _read_activity_ts() -> Optional[float]:
    if not _ACTIVITY.exists():
        return None
    try:
        return float(_ACTIVITY.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _owns_pidfile() -> bool:
    """Return True if the pidfile still points at us."""
    try:
        return int(_PIDFILE.read_text(encoding="utf-8").strip()) == os.getpid()
    except Exception:
        return False


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        global _should_stop
        _should_stop = True
        _log("signal_received", signum=signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


async def _improve_once(session_id: str, dataset: str, config: dict) -> bool:
    """Fire one improve cycle. Returns True on success."""
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        from config import ensure_cognee_ready, ensure_identity  # type: ignore

        await ensure_cognee_ready(config)
        user_id, _ = await ensure_identity(config)

        from uuid import UUID

        import cognee
        from cognee.modules.users.methods import get_user

        user = await get_user(UUID(user_id)) if user_id else None
        await cognee.improve(
            dataset=dataset,
            session_ids=[session_id],
            user=user,
            run_in_background=False,
        )
        return True
    except Exception as exc:
        _log("improve_error", error=str(exc)[:300])
        return False


async def _main_loop(session_id: str, dataset: str, config: dict) -> None:
    _log("started", session=session_id, dataset=dataset, poll=POLL_SECONDS, idle=IDLE_SECONDS)
    last_improved_at = 0.0

    while not _should_stop:
        if _STOPFILE.exists():
            _log("stop_sentinel_seen")
            break
        if not _owns_pidfile():
            _log("pidfile_replaced")
            break

        now = time.time()
        ts = _read_activity_ts()
        if ts is None:
            await asyncio.sleep(POLL_SECONDS)
            continue

        idle_for = now - ts
        time_since_improve = now - last_improved_at
        if idle_for >= IDLE_SECONDS and time_since_improve >= IMPROVE_COOLDOWN:
            _log("idle_trigger", idle_for=round(idle_for, 1))
            ok = await _improve_once(session_id, dataset, config)
            if ok:
                last_improved_at = time.time()
                _log("improve_done")

        await asyncio.sleep(POLL_SECONDS)

    _log("exiting")
    try:
        if _owns_pidfile():
            _PIDFILE.unlink()
    except Exception:
        pass


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
    dataset = bootstrap.get("dataset", "claude_sessions")
    config = bootstrap.get("config", {})
    if not session_id:
        _log("fatal_no_session_id")
        sys.exit(1)

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
    except Exception:
        pass

    _install_signal_handlers()

    try:
        asyncio.run(_main_loop(session_id, dataset, config))
    except Exception as exc:
        _log("fatal_loop_error", error=str(exc)[:300])


if __name__ == "__main__":
    main()
