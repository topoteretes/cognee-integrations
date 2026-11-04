"""The bootstrap executes only repository code and the exact-pinned cognee package.

Plugin-directory policy (the Claude Code plugin was rejected over this): a plugin
may not download a program or install script at setup or first run and execute it.
Two paths in ``session-start.py`` used to: ``curl https://astral.sh/uv/install.sh |
sh`` when uv was missing, and uv's own download of a standalone CPython when no
3.12 was installed. Both are gone from claude-code; these tests keep them gone.

The same bootstrap ships in Codex and Antigravity, so every suite is held to it.
"""

from __future__ import annotations

import re
import sys

import pytest

# --- 1. source-level: nothing is piped into a shell, no installer URL -----------

_FORBIDDEN = (
    re.compile(r"astral\.sh/uv"),
    re.compile(r"\bcurl\b"),
    re.compile(r"\bwget\b"),
    re.compile(r"\|\s*(ba)?sh\b"),
    re.compile(r"_install_uv"),
)


def test_scripts_contain_no_remote_installer(suite):
    offenders: list[str] = []
    for path in sorted(suite.scripts_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for pattern in _FORBIDDEN:
            for match in pattern.finditer(source):
                line = source.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.name}:{line}: {match.group(0)!r}")
    assert not offenders, f"{suite.name}:\n  " + "\n  ".join(offenders)


# --- 2. behaviour: uv may not download an interpreter ---------------------------


@pytest.fixture
def ss(suite, hook_module, monkeypatch):
    module = hook_module(suite, "session-start.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    return module


def test_uv_runs_with_python_downloads_disabled(suite, ss, monkeypatch):
    monkeypatch.setattr(ss, "_find_uv", lambda: "/fake/uv")
    monkeypatch.setattr(ss, "_venv_cognee_version", lambda: "")
    calls: list[tuple[list[str], dict]] = []

    def _record(cmd, *args, **kwargs):
        calls.append(([str(c) for c in cmd], dict(kwargs.get("env") or {})))
        raise OSError("hermetic: no real uv")

    monkeypatch.setattr(ss.subprocess, "run", _record)
    # Keep the stdlib fallback out of the picture: it must not find a host python.
    monkeypatch.setattr(ss, "_find_host_python", lambda: "")
    capsys_marker = ss._HOST_PYTHON_MARKER
    assert ss.ensure_cognee_installed() is False

    uv_calls = [(cmd, env) for cmd, env in calls if cmd[0] == "/fake/uv"]
    assert uv_calls, f"{suite.name}: uv was never invoked"
    cmd, env = uv_calls[0]
    assert cmd[:2] == ["/fake/uv", "venv"]
    assert env.get("UV_PYTHON_DOWNLOADS") == "never"
    # A range request: pick an installed interpreter, never insist on one minor.
    assert cmd[cmd.index("--python") + 1] == ss._PINNED_PYTHON
    assert ss._PINNED_PYTHON.startswith(">=")
    # With uv unable to build the venv and no host python, the user is told.
    assert capsys_marker.exists()


# --- 3. behaviour: the stdlib fallback uses an installed 3.10+, found on PATH ----


def test_fallback_uses_newer_python_from_path_when_host_is_old(suite, ss, temp_home, monkeypatch):
    """macOS: the hook runs under /usr/bin/python3 3.9.6, Homebrew has python3.12."""
    monkeypatch.setattr(ss, "_find_uv", lambda: "")
    monkeypatch.setattr(sys, "version_info", (3, 9, 6, "final", 0))
    fake = "/opt/homebrew/bin/python3.12"
    monkeypatch.setattr(
        ss.shutil, "which", lambda name, *a, **k: fake if name == "python3.12" else None
    )
    monkeypatch.setattr(
        ss, "_host_python_version", lambda python: (3, 12) if python == fake else ()
    )
    calls: list[list[str]] = []

    def _record(cmd, *args, **kwargs):
        calls.append([str(c) for c in cmd])
        raise OSError("hermetic: no real venv build")

    monkeypatch.setattr(ss.subprocess, "run", _record)

    assert ss.ensure_cognee_installed() is False  # the fake build produced no venv
    assert calls and calls[0][:3] == [fake, "-m", "venv"], calls
    assert not ss._HOST_PYTHON_MARKER.exists()


def test_fallback_skips_python_newer_than_cognee_supports(suite, ss, temp_home, monkeypatch):
    """The range is bounded above as well: cognee's wheels refuse a Python past
    3.14, so a 3.15 host is skipped and a 3.12 on PATH is preferred."""
    monkeypatch.setattr(ss, "_find_uv", lambda: "")
    monkeypatch.setattr(sys, "version_info", (3, 15, 0, "final", 0))
    fake = "/opt/homebrew/bin/python3.12"
    monkeypatch.setattr(
        ss.shutil, "which", lambda name, *a, **k: fake if name == "python3.12" else None
    )
    monkeypatch.setattr(
        ss, "_host_python_version", lambda python: (3, 12) if python == fake else ()
    )
    calls: list[list[str]] = []

    def _record(cmd, *args, **kwargs):
        calls.append([str(c) for c in cmd])
        raise OSError("hermetic")

    monkeypatch.setattr(ss.subprocess, "run", _record)
    assert ss.ensure_cognee_installed() is False
    assert calls and calls[0][:3] == [fake, "-m", "venv"], calls


def test_fallback_refuses_when_only_a_too_new_python_exists(suite, ss, temp_home, monkeypatch):
    monkeypatch.setattr(ss, "_find_uv", lambda: "")
    monkeypatch.setattr(sys, "version_info", (3, 15, 0, "final", 0))
    monkeypatch.setattr(ss.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(
        ss.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no build"))
    )
    assert ss.ensure_cognee_installed() is False
    assert ss._HOST_PYTHON_MARKER.exists()
    assert ss._PINNED_PYTHON == ">=3.10,<3.15"  # uv is held to the same range


def test_fallback_runs_when_uv_cannot_find_an_interpreter(suite, ss, monkeypatch):
    """uv present, nothing it may use: fall through to the stdlib venv, don't give up."""
    monkeypatch.setattr(ss, "_find_uv", lambda: "/fake/uv")
    monkeypatch.setattr(ss, "_venv_cognee_version", lambda: "")
    monkeypatch.setattr(ss, "_find_host_python", lambda: sys.executable)
    calls: list[list[str]] = []

    def _record(cmd, *args, **kwargs):
        calls.append([str(c) for c in cmd])
        raise OSError("hermetic")

    monkeypatch.setattr(ss.subprocess, "run", _record)
    assert ss.ensure_cognee_installed() is False
    assert calls[0][:2] == ["/fake/uv", "venv"]
    assert calls[1][:3] == [sys.executable, "-m", "venv"], calls
