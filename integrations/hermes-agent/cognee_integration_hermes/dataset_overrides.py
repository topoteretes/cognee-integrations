"""Per-conversation dataset overrides, persisted across process restarts.

A ``cognee_switch_dataset`` switch must survive the Hermes process: the same
Hermes session resumed later should keep writing where the user moved it, not
silently fall back to the configured default. The claude-code/codex plugins
keep this in their launch records and openclaw in ``dataset-overrides.json``;
this is the hermes counterpart of the latter — one JSON map keyed by the
*hermes* session id::

    { "<hermes session id>": {"dataset": "...", "counter": 2}, ... }

``counter`` feeds the switched cognee session id (``hermes_<id>__N``): a
cognee session never spans two datasets, so every switch mints a fresh one.
Everything here is best-effort — an unreadable file means "no overrides",
never a failed session.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from .config import SHARED_PLUGIN_STATE_DIR

logger = logging.getLogger(__name__)

_OVERRIDES_PATH = SHARED_PLUGIN_STATE_DIR / "hermes" / "dataset-overrides.json"

# Overrides for conversations nobody resumes must not accumulate forever.
_MAX_ENTRIES = 200


def _load(path: Optional[Path] = None) -> dict[str, Any]:
    target = path or _OVERRIDES_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, Any], path: Optional[Path] = None) -> None:
    target = path or _OVERRIDES_PATH
    try:
        if len(data) > _MAX_ENTRIES:
            # Oldest first by recorded time; unstamped entries drop first.
            ordered = sorted(data.items(), key=lambda kv: kv[1].get("updated_at", 0))
            data = dict(ordered[-_MAX_ENTRIES:])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        logger.debug("could not persist dataset overrides: %s", exc)


def load_override(session_id: str, *, path: Optional[Path] = None) -> dict[str, Any]:
    """The stored override for one hermes session id, or {}."""
    if not session_id:
        return {}
    entry = _load(path).get(session_id)
    return entry if isinstance(entry, dict) else {}


def save_override(
    session_id: str, dataset: str, counter: int, *, path: Optional[Path] = None
) -> None:
    if not session_id:
        return
    import time

    data = _load(path)
    data[session_id] = {"dataset": dataset, "counter": counter, "updated_at": time.time()}
    _save(data, path)


def clear_override(session_id: str, *, path: Optional[Path] = None) -> None:
    data = _load(path)
    if session_id in data:
        del data[session_id]
        _save(data, path)
