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

The cap is ``COGNEE_PLUGIN_LOG_MAX_BYTES`` (bytes; default 20 MiB), the same for every
log — a number small enough to open and grep comfortably and large enough for
months of normal hook traffic (hook.log runs at roughly 0.5 MB a day). ``0``
disables rotation. (Not ``COGNEE_LOG_MAX_BYTES``: cognee itself reads that one
for its own file-log rotation, and the spawned server inherits our environment.)
Everything here is best-effort and never raises: a log we
cannot rotate or write must never be the reason a hook fails.

Kept stdlib-only, like ``_proc`` and ``_env_file``, so the detached watchers can
import it without the rest of the plugin.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_MAX_BYTES = 20 * 1024 * 1024
MAX_BYTES_ENV = "COGNEE_PLUGIN_LOG_MAX_BYTES"
#: Suffix of the single kept generation.
ROTATED_SUFFIX = ".1"


def max_bytes(default: int | None = None) -> int:
    """The cap in bytes: ``COGNEE_PLUGIN_LOG_MAX_BYTES`` when set and numeric, else ``default``."""
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


# ---------------------------------------------------------------------------
# Console capture for a child that outlives us (the local cognee server).
# ---------------------------------------------------------------------------
#
# The server's console output is a stream nobody can cap after the fact: the
# worker that spawns it exits as soon as /health answers, and cognee's console
# handler cannot be switched off. Sending it to a file recreated the gigabyte
# log; sending it to /dev/null lost the one thing that matters — the traceback
# from a boot that never got as far as opening its own log (import error,
# broken venv, port already bound). So the child writes into a pipe read by
# this pump: a detached, stdlib-only process that copies the first ``cap``
# bytes of each boot to ``server-console.log`` (the previous boot's capture is
# kept as ``.1``), then keeps draining the pipe and discards the rest until the
# child exits. The server never blocks on a full pipe, and the file is bounded
# by construction.

#: Default capture per boot. Every boot failure ever seen fit in a few KB; a
#: healthy boot's chatter fills this in minutes and is then discarded.
SERVER_CONSOLE_CAP_BYTES = 1024 * 1024

_CONSOLE_PUMP_SOURCE = r"""
import os, sys
path, cap = sys.argv[1], int(sys.argv[2])
try:
    os.replace(path, path + ".1")
except OSError:
    pass
out = open(path, "ab")
written = 0
while True:
    try:
        chunk = os.read(0, 65536)
    except OSError:
        break
    if not chunk:
        break
    if written < cap:
        take = chunk[: cap - written]
        out.write(take)
        out.flush()
        written += len(take)
        if written >= cap:
            out.write(
                b"\n[cognee-plugin: console capture cap reached; "
                b"the rest of this boot's output is discarded]\n"
            )
            out.flush()
out.close()
"""


def start_console_capture(path, cap: int = SERVER_CONSOLE_CAP_BYTES):
    """Start the capture pump; returns the ``Popen`` whose ``stdin`` is the sink
    to hand a child as its stdout/stderr, or ``None`` when the pump could not be
    started (callers then fall back to discarding the child's output). Close
    ``pump.stdin`` in the parent once the child holds it, or the pump never
    sees EOF."""
    import subprocess
    import sys

    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return subprocess.Popen(
            [sys.executable, "-c", _CONSOLE_PUMP_SOURCE, str(path), str(int(cap))],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return None


def console_capture_tail(path, max_bytes: int = 2000) -> str:
    """The last ``max_bytes`` of a capture file, for error messages. '' if none."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return data[-max_bytes:].decode("utf-8", "replace").strip()
