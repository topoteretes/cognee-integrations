#!/usr/bin/env python3
"""Run one hook script so that a crash is reported instead of swallowed.

hooks.json launches every hook as ``python hook_runner.py <script> [args...]``.
Without this, anything that fails before a hook's own ``try`` (an import-time
error in ``_plugin_common``, an unreadable stdin, an unsupported interpreter)
exits the interpreter with status 1. The host then shows at most "Hook
failed" (nothing at all for an async hook), no traceback reaches ``hook.log``,
and the ``|| python`` fallback in hooks.json runs the hook a second time.

The runner:

* forces UTF-8 on the hook's stdio (Windows pipes default to the ANSI code
  page, which can neither decode every prompt nor encode every reply);
* runs the script as ``__main__`` with the script as ``sys.argv[0]``, exactly as
  ``python <script>`` would;
* on an uncaught exception, appends the traceback to
  ``~/.cognee-plugin/<plugin>/hook-crash.log`` (the temp dir if no home can be
  resolved), prints one line to stderr, surfaces a rate-limited
  ``systemMessage`` so the user learns memory is off, and exits 0;
* leaves an explicit ``sys.exit(code)`` from the script untouched.

Stdlib only, and nothing from the plugin is imported here: this file has to
work precisely when the plugin's own modules don't. The Codex and Claude Code
plugins carry identical copies apart from ``_STATE_SUBDIR``.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
import tempfile
import time
import traceback

#: The plugin's state dir under ~/.cognee-plugin (the only line that differs per plugin).
_STATE_SUBDIR = "claude-code"
_MIN_PYTHON = (3, 9)
_CRASH_LOG_NAME = "hook-crash.log"
_CRASH_LOG_MAX_BYTES = 1_000_000
_REPORTED_NAME = "hook-crash-reported.json"
# One systemMessage per distinct crash per hour: PostToolUse fires on every
# tool call, and repeating the same message each time would drown the session.
_REPORT_INTERVAL_SECONDS = 3600
#: Read by _plugin_common._reexec_into_venv so the venv re-exec keeps the runner.
RUNNER_ENV = "COGNEE_HOOK_RUNNER"


def _log_dir() -> str:
    home = os.path.expanduser("~")
    if home and home != "~":
        return os.path.join(home, ".cognee-plugin", _STATE_SUBDIR)
    return tempfile.gettempdir()


def _force_utf8_stdio() -> None:
    for name, errors in (
        ("stdin", "surrogateescape"),
        ("stdout", "surrogateescape"),
        ("stderr", "backslashreplace"),
    ):
        stream = getattr(sys, name, None)
        encoding = (
            (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        )
        if encoding == "utf8" or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors=errors)
        except Exception:
            pass  # a stream that can't be reconfigured keeps working as it was


class _WriteTracker:
    """Proxy for sys.stdout that remembers whether the hook printed anything.

    A crash report goes to stdout as JSON only when stdout is still empty, so it
    can never be appended to half a reply and corrupt what the host parses.
    """

    def __init__(self, stream):
        self._stream = stream
        self.written = False

    def write(self, data):
        if data:
            self.written = True
        return self._stream.write(data)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _append_crash_log(log_dir: str, script: str, detail: str) -> str:
    path = os.path.join(log_dir, _CRASH_LOG_NAME)
    try:
        os.makedirs(log_dir, exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > _CRASH_LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        header = "--- {} {} pid={} python={} ({})\n".format(
            stamp,
            os.path.basename(script),
            os.getpid(),
            ".".join(str(part) for part in sys.version_info[:3]),
            sys.executable,
        )
        with open(path, "a", encoding="utf-8", errors="backslashreplace") as handle:
            handle.write(header + detail.rstrip("\n") + "\n")
    except Exception:
        pass
    return path


def _should_report(log_dir: str, signature: str) -> bool:
    path = os.path.join(log_dir, _REPORTED_NAME)
    now = time.time()
    try:
        with open(path, encoding="utf-8") as handle:
            reported = json.load(handle)
        if not isinstance(reported, dict):
            reported = {}
    except Exception:
        reported = {}
    last = reported.get(signature)
    if isinstance(last, (int, float)) and now - last < _REPORT_INTERVAL_SECONDS:
        return False
    reported = {
        key: value
        for key, value in reported.items()
        if isinstance(value, (int, float)) and now - value < _REPORT_INTERVAL_SECONDS
    }
    reported[signature] = now
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(reported, handle)
    except Exception:
        pass
    return True


def _report(script: str, summary: str, detail: str, stdout: _WriteTracker) -> int:
    log_dir = _log_dir()
    log_path = _append_crash_log(log_dir, script, detail)
    name = os.path.basename(script)
    message = "Cognee memory: hook {} failed ({}). Details: {}".format(name, summary, log_path)
    try:
        sys.stderr.write("cognee-plugin: {}\n".format(message))
    except Exception:
        pass
    if not stdout.written and _should_report(log_dir, "{}|{}".format(name, summary)):
        try:
            stdout.write(json.dumps({"systemMessage": message}) + "\n")
            stdout.flush()
        except Exception:
            pass
    return 0


def main(argv: list) -> int:
    _force_utf8_stdio()
    stdout = _WriteTracker(sys.stdout)
    sys.stdout = stdout

    if len(argv) < 2:
        sys.stderr.write("usage: hook_runner.py <hook-script.py> [args...]\n")
        return 0
    script = os.path.abspath(argv[1])

    if sys.version_info < _MIN_PYTHON:
        summary = "Python {}.{} is too old; the hooks need {}.{} or newer".format(
            sys.version_info[0], sys.version_info[1], *_MIN_PYTHON
        )
        return _report(script, summary, summary + ": " + sys.executable, stdout)

    os.environ[RUNNER_ENV] = os.path.abspath(__file__)
    sys.argv = [script, *argv[2:]]
    script_dir = os.path.dirname(script)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit:
        raise  # the script chose its exit status
    except Exception as exc:
        summary = "{}: {}".format(type(exc).__name__, str(exc)[:200])
        return _report(script, summary, traceback.format_exc(), stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
