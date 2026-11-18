"""What the hooks and the hooks module (hooks/register.ts) tell each other.

The module draws the status row and the dataset picker. It starts no program
and opens no connection: it only reads files the plugin's scripts write and
keeps a few values in its own store. This file is the scripts' side of that.

From the scripts to the module, in ``~/.cognee-plugin/claude-code``:

* ``status/<session>.json``: the status row's data, written by the status
  line renderer (``cognee_statusline_render.write_status``);
* ``mod/<session>.json``: the datasets the picker offers, the recall mode, and
  what became of the last pick (``write_bridge``).

From the module to the scripts, through the module's store, a JSON file Claude
Code keeps under its own config directory (``plugins/store``):

* ``band:<session>``: the module draws this session's row, so the settings
  status line prints nothing for it;
* ``pick:<session>``: the dataset the person picked. The prompt hook applies
  it before it recalls (``apply_pick``), which is why a pick takes effect with
  the next message.

Claude Code does not document where the store lives. If it moves, a pick is
never seen here; the module then says so and points at the
cognee-switch-datasets skill, and the status line shows beside the band.

Stdlib only, and nothing from the plugin is imported at module level: the
status line renderer imports this file and must stay standalone.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_STATE_DIR = Path.home() / ".cognee-plugin" / "claude-code"
_BRIDGE_DIR = _STATE_DIR / "mod"
_SCRIPTS = Path(__file__).resolve().parent

#: The plugin's name, which the store's file name starts with.
_PLUGIN_NAME = "cognee-memory"
#: A listing older than this is refreshed at the next prompt.
LISTING_MAX_AGE_SECONDS = 600.0
_LIST_TIMEOUT_SECONDS = 45.0
_SWITCH_TIMEOUT_SECONDS = 100.0


def _path_safe(session_id: str) -> bool:
    return bool(session_id) and all(c.isalnum() or c in "-_." for c in session_id)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(json.dumps(value), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------- the store


def store_dir() -> Path:
    """Claude Code's store directory for mods (``CLAUDE_CONFIG_DIR`` or ``~/.claude``)."""
    config = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(config) if config else Path.home() / ".claude"
    return base / "plugins" / "store"


def store_values() -> dict:
    """Every value the module saved, from each install of this plugin.

    One file per install (the marketplace copy, a ``--plugin-dir`` copy): the
    newest file wins where two hold the same key. Empty when the store cannot
    be found or read.
    """
    try:
        files = sorted(store_dir().glob(_PLUGIN_NAME + "_*.json"), key=lambda p: p.stat().st_mtime)
    except Exception:
        return {}
    merged: dict = {}
    for path in files:
        merged.update(_read_json(path))
    return merged


def band_drawn(session_id: str) -> bool:
    """True when the hooks module says it draws this session's row."""
    if not _path_safe(session_id):
        return False
    return isinstance(store_values().get("band:" + session_id), dict)


def pending_pick(session_id: str) -> dict:
    """The dataset pick the module saved for this session, or {}."""
    if not _path_safe(session_id):
        return {}
    pick = store_values().get("pick:" + session_id)
    if not isinstance(pick, dict) or not str(pick.get("target") or "").strip():
        return {}
    return pick


# ------------------------------------------------- the file the module reads


def bridge_path(session_id: str) -> Path:
    return _BRIDGE_DIR / (session_id + ".json")


def read_bridge(session_id: str) -> dict:
    return _read_json(bridge_path(session_id)) if _path_safe(session_id) else {}


def write_bridge(session_id: str, **updates) -> None:
    """Merge ``updates`` into this session's bridge file. Never raises."""
    if not _path_safe(session_id):
        return
    try:
        current = read_bridge(session_id)
        current.update(updates)
        _write_json(bridge_path(session_id), current)
    except Exception:
        pass


def _run_switch(session_id: str, *args: str, timeout: float) -> dict:
    """One run of switch-dataset.py for this session, as its JSON answer."""
    command = [
        sys.executable,
        str(_SCRIPTS / "switch-dataset.py"),
        *args,
        "--json",
        "--session-key",
        session_id,
    ]
    try:
        ran = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": "the dataset script gave no answer within %d s" % int(timeout)}
    except Exception as exc:
        return {"error": "could not run the dataset script: %s" % str(exc)[:200]}
    try:
        answer = json.loads(ran.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        answer = {}
    if not isinstance(answer, dict) or not answer:
        return {"error": "the dataset script gave no answer"}
    return answer


def refresh_listing(session_id: str) -> bool:
    """List the datasets the picker offers and save them for the module."""
    if not _path_safe(session_id):
        return False
    listing = _run_switch(session_id, "--list", timeout=_LIST_TIMEOUT_SECONDS)
    if listing.get("error"):
        write_bridge(
            session_id, listing_error=str(listing["error"])[:300], listing_tried=time.time()
        )
        return False
    write_bridge(session_id, listing=listing, listing_at=time.time(), listing_error="")
    return True


def listing_is_stale(session_id: str) -> bool:
    bridge = read_bridge(session_id)
    try:
        age = time.time() - float(bridge.get("listing_at") or 0)
    except (TypeError, ValueError):
        return True
    return not isinstance(bridge.get("listing"), dict) or age > LISTING_MAX_AGE_SECONDS


def refresh_listing_in_background(session_id: str) -> None:
    """Start ``refresh_listing`` in a process of its own, so a hook does not wait."""
    if not _path_safe(session_id):
        return
    kwargs = (
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "refresh-listing", session_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except Exception:
        pass


def apply_pick(session_id: str) -> dict:
    """Carry out the pick the module saved, once. Returns what was recorded, or {}.

    Called by the prompt hook before it recalls, so the message that follows a
    pick is already answered from the new dataset. A pick is identified by the
    time the module saved it (``at``): one that was already handled is skipped.
    """
    pick = pending_pick(session_id)
    if not pick:
        return {}
    at = pick.get("at")
    if read_bridge(session_id).get("pick", {}).get("at") == at:
        return {}
    target = str(pick.get("target")).strip()
    answer = _run_switch(session_id, target, timeout=_SWITCH_TIMEOUT_SECONDS)
    failed = bool(answer.get("error")) or (
        answer.get("switched") is not True and answer.get("switched") is not False
    )
    result = {
        "at": at,
        "target": target,
        "state": "failed" if failed else "done",
        "message": str(answer.get("error") or answer.get("message") or "")[:300],
        "dataset": str(answer.get("dataset") or ""),
        "recall": str(answer.get("recall") or ""),
        "handled_at": time.time(),
    }
    write_bridge(session_id, pick=result)
    if not failed:
        refresh_listing(session_id)
    return result


def refresh_status(session_id: str) -> None:
    """Rewrite the status row's data for the module. Never raises."""
    if not _path_safe(session_id):
        return
    try:
        import cognee_statusline_render

        cognee_statusline_render.write_status(session_id)
    except Exception:
        pass


def main(argv: list) -> int:
    if len(argv) == 3 and argv[1] == "refresh-listing":
        refresh_listing(argv[2])
        refresh_status(argv[2])
        return 0
    sys.stderr.write("usage: _mod_bridge.py refresh-listing <session id>\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
