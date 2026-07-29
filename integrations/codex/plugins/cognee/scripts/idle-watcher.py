#!/usr/bin/env python3
"""Idle watcher daemon - persists quiet Codex sessions into Cognee.

Launched detached from ``session-start.py``. Polls
``~/.cognee-plugin/codex/activity.ts`` every ``POLL_SECONDS``. When the last
activity is older than ``IDLE_SECONDS`` and we haven't bridged since
that point, persists the session cache and refreshes graph context.

Stops cleanly on:
  * ``~/.cognee-plugin/codex/watcher.stop`` sentinel file.
  * Receiving SIGTERM (from SessionEnd hook or manual kill).
  * The pidfile being overwritten by a newer watcher (restart case).

Survives Codex crashes better than foreground hooks.
"""

import asyncio
import json
import os
import signal
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Optional

# Tunable via env. Defaults chosen to avoid thrashing the LLM: 60s idle
# threshold means you have to actively pause a full minute, and the 10-minute
# improve cooldown prevents back-to-back improve runs when activity is sporadic.
POLL_SECONDS = float(os.environ.get("COGNEE_IDLE_POLL", "10"))
IDLE_SECONDS = float(os.environ.get("COGNEE_IDLE_THRESHOLD", "60"))
IMPROVE_COOLDOWN = float(os.environ.get("COGNEE_IMPROVE_COOLDOWN", "600"))

_PLUGIN_DIR = Path.home() / ".cognee-plugin" / "codex"
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
    except Exception as exc:
        _log("activity_read_failed", error=str(exc)[:200])
        return None


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


async def _improve_once(session_id: str, dataset: str, config: dict) -> bool:
    """Fire one session improve cycle. Returns True on success."""
    sys.path.insert(0, os.path.dirname(__file__))
    try:
        from _plugin_common import (  # type: ignore
            http_api_ready,
            load_resolved,
            resolve_user,
            run_session_improve,
            set_session_key,
            sync_lock,
        )

        session_key = str(config.get("session_key") or "").strip()
        if session_key:
            set_session_key(session_key)
        api_mode = http_api_ready()
        # Server-side improve has its own per-session lock; only local SDK
        # mode needs the cross-hook file lock.
        lock = nullcontext(True) if api_mode else sync_lock("idle-watcher")
    except Exception as exc:
        _log("sync_lock_import_error", error=str(exc)[:200])
        api_mode = False
        lock = nullcontext(True)

    with lock as acquired:
        if not acquired:
            _log("bridge_skipped_lock_busy", session=session_id, dataset=dataset)
            return False

        try:
            from config import (  # type: ignore
                ensure_cognee_ready,
                ensure_dataset_ready,
                ensure_identity,
                improve_session_local,
            )

            if api_mode:
                wrote = run_session_improve(dataset, session_id)
                _log(
                    "session_bridge_done",
                    session=session_id,
                    dataset=dataset,
                    via="http_improve",
                    wrote=wrote,
                )
                return True

            await ensure_cognee_ready(config)
            user_id = str(config.get("user_id") or load_resolved().get("user_id") or "")
            if not user_id:
                user_id, _ = await ensure_identity(config)

            user = await resolve_user(user_id) if user_id else None
            if user:
                await ensure_dataset_ready(dataset, user)
                result = await improve_session_local(dataset, session_id, user)
                _log(
                    "session_bridge_done",
                    session=session_id,
                    dataset=dataset,
                    user_id=str(user.id),
                    via="local_improve",
                    ok=bool(result.get("ok")),
                )
            return True
        except Exception as exc:
            _log("bridge_error", error=str(exc)[:300])
            return False


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
            get_session_key,
            read_llm_state,
            service_url_is_local,
            write_llm_state,
        )

        base_url = str(config.get("base_url") or "")
        if base_url and not service_url_is_local(base_url):
            return  # cloud: the remote server owns its own LLM key

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
        idle=IDLE_SECONDS,
    )
    # Runs once per watcher launch (≈ once per session); the check itself is
    # internally rate-limited to ≤ once per COGNEE_UPDATE_CHECK_INTERVAL.
    _run_update_check()
    # Validate the LLM key once at session start (background, provider-agnostic).
    _check_llm_key(config)
    last_improved_at = 0.0
    exit_reason = "loop_complete"
    bridge_disabled = False

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
        ts = _read_activity_ts()
        if ts is None:
            await asyncio.sleep(POLL_SECONDS)
            continue

        idle_for = now - ts
        time_since_improve = now - last_improved_at
        if (
            not bridge_disabled
            and idle_for >= IDLE_SECONDS
            and time_since_improve >= IMPROVE_COOLDOWN
        ):
            _log("idle_trigger", idle_for=round(idle_for, 1))
            ok = await _improve_once(session_id, dataset, config)
            if ok:
                last_improved_at = time.time()
                _log("bridge_done")
                exit_reason = "bridge_complete"
                break
            bridge_disabled = True
            _log("bridge_disabled_after_failure")

        await asyncio.sleep(POLL_SECONDS)

    if _should_stop:
        exit_reason = "signal"

    ts = _read_activity_ts()
    if (
        not bridge_disabled
        and exit_reason in {"signal", "stop_sentinel"}
        and ts
        and ts > last_improved_at
    ):
        _log("shutdown_trigger", reason=exit_reason, activity_age=round(time.time() - ts, 1))
        ok = await _improve_once(session_id, dataset, config)
        if ok:
            last_improved_at = time.time()
            _log("shutdown_bridge_done")
        else:
            _log("shutdown_bridge_failed")

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
