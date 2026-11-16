"""The SessionStart sweep of dead per-session state, split out of
_plugin_common.py to keep that file under the plugin directory's per-file
size limit.

Import it through _plugin_common, which re-exports every name here. Names
that live in _plugin_common are looked up there at call time (``_pc.<name>``),
so a monkeypatch on _plugin_common reaches this module too.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import _plugin_common as _pc
import _proc
from _logfiles import rotate_if_oversized as _rotate_log_if_oversized

# ---------------------------------------------------------------------------
# State sweep — the per-session files nothing else ever deletes.
# ---------------------------------------------------------------------------
#
# Every launch writes one file into several directories (a launch record, a
# connection marker, an LLM-key verdict, last-recall counts, a bridge cache, a
# pending-prompt buffer, maybe an improve lock). Nothing removed them, so a
# machine in daily use accumulated ~1,200 of them in two months. Each is
# useless once its session is over; the sweep runs at SessionStart and removes
# the ones whose session is provably gone. The rules are deliberately lazy —
# days, not minutes — because the only cost of a stale file is clutter, while
# a premature delete can lose a final sync. Same ownership rule as everywhere
# else in the state dir: this touches only THIS plugin's subdirectory, plus the
# one shared marker this plugin itself writes.

#: Status markers (conn-state, llm-state, recall) and the per-session bridge
#: cache / pending-prompt buffer: gone after a week without a write. A live
#: session rewrites its markers on every hook, so age alone is a safe signal.
_SWEEP_SESSION_FILE_MAX_AGE_SECONDS = 7 * 24 * 3600
#: Launch records: removed a week after their host pid is dead, or after 30
#: days regardless (a record with no pid is treated as alive, so that is the
#: only bound for those). The functional reader — the exit-watcher's final
#: sync — needs the record for seconds after the host exits, but a human
#: debugging a Friday crash on Monday needs it for days; a week matches the
#: marker rule above so there is one number to remember. Costs nothing:
#: `_live_launch_records` already ignores dead-pid records at read time.
_SWEEP_LAUNCH_RECORD_DEAD_GRACE_SECONDS = 7 * 24 * 3600
_SWEEP_LAUNCH_RECORD_MAX_AGE_SECONDS = 30 * 24 * 3600
#: Logs the sweep rotates when over the cap. Most are also rotated by their own
#: writer; this catches files that predate the cap and logs only ever written
#: by a child process.
_SWEEP_LOG_FILES = (
    "cloud-worker.log",
    "hook.log",
    "bootstrap.log",
    "watcher.log",
    "exit-watcher.log",
    "subprocess.log",
    "recall-audit.log",
    "activity.log",
)
#: Directories older plugin versions created here and nothing reads any more.
#: improve-locks: the machine-wide per-session improve lock removed in 1.5.3.
_SWEEP_LEGACY_DIRS = ("statusline", "improve-locks")
#: Files likewise. recall-breaker.json: cognee-search.sh used to redirect the
#: circuit breaker into this dir, splitting it from the one the hooks use.
_SWEEP_LEGACY_FILES = ("recall-breaker.json",)


def _sweep_remove(path: Path, counts: dict, key: str) -> None:
    try:
        path.unlink()
        counts[key] = counts.get(key, 0) + 1
    except FileNotFoundError:
        pass  # a concurrent SessionStart swept it first
    except OSError as exc:
        counts.setdefault("errors", []).append(f"{path.name}: {str(exc)[:80]}")


def _sweep_dir_by_age(directory: Path, max_age: float, now: float, counts: dict, key: str) -> None:
    try:
        entries = list(directory.glob("*.json"))
    except OSError:
        return
    for path in entries:
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age > max_age:
            _sweep_remove(path, counts, key)


def _sweep_pending_husks(counts: dict) -> None:
    """Empty ``{}`` pending files left by older versions of ``pop_pending_prompt``.
    Nothing is in flight for an empty buffer, so age is irrelevant; a live
    session that needs the file again simply recreates it."""
    try:
        entries = list(_pc._PENDING_DIR.glob("*.json"))
    except OSError:
        return
    for path in entries:
        try:
            if path.stat().st_size <= 2 and not _pc._load_json_file(path):
                _sweep_remove(path, counts, "pending_husks")
        except OSError:
            continue


def _sweep_launch_records(now: float, counts: dict) -> None:
    try:
        entries = list(_pc._SESSIONS_MAP_DIR.glob("*.json"))
    except OSError:
        return
    for path in entries:
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age > _SWEEP_LAUNCH_RECORD_MAX_AGE_SECONDS:
            _sweep_remove(path, counts, "launch_records")
            continue
        if age <= _SWEEP_LAUNCH_RECORD_DEAD_GRACE_SECONDS:
            continue
        rec = _pc._load_json_file(path)
        try:
            pid = int((rec or {}).get("host_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0 and not _proc.pid_alive(pid):
            _sweep_remove(path, counts, "launch_records")


def sweep_stale_state(now: Optional[float] = None) -> dict:
    """Remove this plugin's dead per-session files and legacy leftovers.

    Returns a count per category (only non-zero ones, plus ``errors`` when
    something could not be removed). Never raises: a sweep that fails must
    never cost a SessionStart. Logs one ``state_sweep`` event when it did
    anything.
    """
    counts: dict = {}
    now = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    try:
        for directory, key in (
            (_pc._CONN_STATE_DIR, "conn_state"),
            (_pc._LLM_STATE_DIR, "llm_state"),
            (_pc._PLUGIN_DIR / "recall", "recall"),
            (_pc._BRIDGE_DIR, "bridge"),
            (_pc._PENDING_DIR, "pending"),
            (_pc._IMPROVE_STATE_DIR, "improve_state"),
            (_pc._PLUGIN_DIR / "spool" / "expired", "expired_cloud_work"),
            (_pc._PLUGIN_DIR / "improve-wanted" / "expired", "expired_cloud_work"),
            # The live-activity marker each session's hooks write, and the
            # band marker the Claude Code mod writes beside it: one file per
            # session (every /clear and resume), rewritten only while it runs.
            (_pc._PHASE_DIR, "phase"),
            (_pc._PLUGIN_DIR / "band", "band"),
        ):
            _sweep_dir_by_age(directory, _SWEEP_SESSION_FILE_MAX_AGE_SECONDS, now, counts, key)
        sessions = _pc._PLUGIN_DIR / "sessions"
        if sessions.is_dir():
            for directory in sessions.iterdir():
                if not directory.is_dir() or directory.is_symlink():
                    continue
                # One session directory must not stop the rest of the sweep
                # (log rotation included): a temp file can vanish between
                # iterdir() and stat(), and a directory may not be removable.
                try:
                    newest = max((p.stat().st_mtime for p in directory.iterdir()), default=0)
                    if now - newest > _SWEEP_SESSION_FILE_MAX_AGE_SECONDS:
                        shutil.rmtree(directory)
                        counts["sessions"] = counts.get("sessions", 0) + 1
                except OSError as exc:
                    counts.setdefault("errors", []).append(
                        f"sessions/{directory.name}: {str(exc)[:80]}"
                    )
        _sweep_pending_husks(counts)
        _sweep_launch_records(now, counts)
        for name in _SWEEP_LEGACY_FILES:
            legacy_file = _pc._PLUGIN_DIR / name
            if legacy_file.is_file():
                _sweep_remove(legacy_file, counts, "legacy_files")
        for name in _SWEEP_LEGACY_DIRS:
            legacy = _pc._PLUGIN_DIR / name
            if legacy.is_dir():
                try:
                    shutil.rmtree(legacy)
                    counts["legacy_dirs"] = counts.get("legacy_dirs", 0) + 1
                except OSError as exc:
                    counts.setdefault("errors", []).append(f"{name}/: {str(exc)[:80]}")
        for name in _SWEEP_LOG_FILES:
            if _rotate_log_if_oversized(_pc._PLUGIN_DIR / name):
                counts["logs_rotated"] = counts.get("logs_rotated", 0) + 1
    except Exception as exc:  # pragma: no cover - defensive
        counts.setdefault("errors", []).append(str(exc)[:120])
    if counts:
        _pc.hook_log("state_sweep", counts)
    return counts
