#!/usr/bin/env python3
"""Bounded append-only logs for the plugin state dir.

Every log the plugin keeps — hook.log, watcher.log, subprocess.log,
exit-watcher.log, recall-audit.log, bootstrap.log, activity.log — used to be a
plain ``open("a")`` with no ceiling. A hook that loops, a watcher that logs once a
minute for months, or a server that prints a traceback per request then grows the
file until the disk complains; bootstrap.log has been seen at several gigabytes,
which no editor opens and no one reads. Diagnostics only need the recent past.

Two entry points, for the two ways a log gets written:

* :func:`append_line` — for code that writes lines itself. Rotates first when the
  file is over the cap, then appends.
* :func:`rotate_if_oversized` — for logs handed to a child process as its
  stdout/stderr (the detached workers). The writer is another process, so the
  only place to enforce the cap is the moment the file is opened for it.

Rotation keeps exactly one previous generation (``<name>.1``): the useful part of
a failure is usually the stretch right before the cap hit, and that survives the
rotation. Two generations bound every log at ``2 * cap``.

The cap is ``COGNEE_LOG_MAX_BYTES`` (bytes; default 20 MiB), the same for every
log — a number small enough to open and grep comfortably and large enough for
months of normal hook traffic (hook.log runs at roughly 0.5 MB a day). ``0``
disables rotation. Everything here is best-effort and never raises: a log we
cannot rotate or write must never be the reason a hook fails.

Kept stdlib-only, like ``_proc`` and ``_env_file``, so the detached watchers can
import it without the rest of the plugin.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MAX_BYTES = 20 * 1024 * 1024
MAX_BYTES_ENV = "COGNEE_LOG_MAX_BYTES"
#: Suffix of the single kept generation.
ROTATED_SUFFIX = ".1"


def max_bytes(default: int | None = None) -> int:
    """The cap in bytes: ``COGNEE_LOG_MAX_BYTES`` when set and numeric, else ``default``."""
    fallback = DEFAULT_MAX_BYTES if default is None else default
    raw = os.environ.get(MAX_BYTES_ENV, "").strip()
    if not raw:
        return fallback
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return fallback


def rotated_path(path) -> Path:
    path = Path(path)
    return path.with_name(path.name + ROTATED_SUFFIX)


def rotate_if_oversized(path, cap: int | None = None) -> bool:
    """Move ``path`` to ``path.1`` when it is larger than the cap.

    Replaces any previous ``.1`` (one generation, not a growing chain). Returns
    True when a rotation happened. Never raises.
    """
    limit = max_bytes() if cap is None else cap
    if limit <= 0:
        return False
    path = Path(path)
    try:
        if path.stat().st_size <= limit:
            return False
        os.replace(path, rotated_path(path))
        return True
    except OSError:
        # Missing file, a concurrent rotation that got there first, or a
        # filesystem that refuses: the append proceeds either way.
        return False


def append_line(path, text: str, cap: int | None = None) -> bool:
    """Append ``text`` (a newline is added if missing) to a capped log.

    Creates the parent directory, rotates when over the cap, then appends.
    Returns True when the line was written. Never raises.
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_if_oversized(path, cap)
        if not text.endswith("\n"):
            text += "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)
        return True
    except Exception:
        return False
