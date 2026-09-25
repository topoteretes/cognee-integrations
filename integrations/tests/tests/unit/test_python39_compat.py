"""The hook layer runs on Python 3.9; only the uv-less venv fallback needs 3.10 (SDK-617).

The hooks are launched as ``python3 <script>`` under whatever python3 the host
has on PATH — 3.9.6 on a Mac with just the Xcode Command Line Tools. They never
import cognee (cognee runs in the uv-managed 3.12 venv the bootstrap builds), so
nothing in them needs 3.10 at runtime. What broke on 3.9 was ``X | None`` in
annotations, which Python evaluates when it defines a function unless the module
defers annotations with ``from __future__ import annotations``. One missing
import in a shared module took every hook down at import time, and the failure
was a ``TypeError`` traceback in a log nobody reads.

Two invariants, asserted for every suite:

1. Every script in the scripts dir defers annotation evaluation and parses under
   the 3.9 grammar (no ``match``, no parenthesized context managers). This is
   the cheap guard that runs on every interpreter; the 3.9 CI cell proves the
   suite as a whole.
2. The one place the host version truly matters — the stdlib ``venv`` fallback
   in ``ensure_cognee_installed`` when uv is unavailable — refuses a host older
   than 3.10 loudly: ``hook.log`` event, stderr line naming the interpreter, and
   a marker that ``session-start.py`` turns into a systemMessage on every launch
   until a venv exists.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from utils.hooklog import hook_events
from utils.suites import plugin_root

_FUTURE = "annotations"
_MARKER_NAME = "host-python-unsupported.json"


# --- 1. source-level invariant -----------------------------------------------


def _defers_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == _FUTURE for alias in node.names)
        for node in tree.body
    )


def test_every_script_parses_on_39_and_defers_annotations(suite):
    """No hook may reintroduce a 3.10-only construct; the import must stay."""
    scripts = sorted(suite.scripts_dir.glob("*.py"))
    assert scripts, f"{suite.name}: no scripts found under {suite.scripts_dir}"
    offenders: list[str] = []
    for path in scripts:
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path), feature_version=(3, 9))
        except SyntaxError as exc:
            offenders.append(f"{path.name}: not valid Python 3.9 (line {exc.lineno}: {exc.msg})")
            continue
        if not _defers_annotations(tree):
            offenders.append(f"{path.name}: missing `from __future__ import annotations`")
    assert not offenders, f"{suite.name}:\n  " + "\n  ".join(offenders)


# --- 2. the uv-less fallback gate --------------------------------------------


@pytest.fixture
def ss(suite, hook_module, monkeypatch):
    module = hook_module(suite, "session-start.py")
    # No uv anywhere, and no network to fetch it: the only remaining install
    # path is the stdlib venv built from the host interpreter.
    monkeypatch.setattr(module, "_find_uv", lambda: "")
    monkeypatch.setattr(module, "_install_uv", lambda: "")
    return module


def _marker(temp_home: Path) -> Path:
    return plugin_root(temp_home) / _MARKER_NAME


def _events(suite, temp_home: Path, name: str) -> list[dict]:
    return [detail for event, detail in hook_events(suite, temp_home) if event == name]


def test_fallback_refuses_host_python_below_310(suite, ss, temp_home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "version_info", (3, 9, 6, "final", 0))

    def _no_subprocess(*args, **kwargs):  # pragma: no cover - asserts the gate held
        raise AssertionError(f"venv build attempted on a 3.9 host: {args[0]!r}")

    monkeypatch.setattr(ss.subprocess, "run", _no_subprocess)

    assert ss.ensure_cognee_installed() is False

    # stderr names the interpreter, its version and the floor — the acceptance
    # criteria of SDK-617 for a "loud" failure.
    err = capsys.readouterr().err
    assert "Python 3.10 or newer" in err
    assert sys.executable in err
    assert "3.9.6" in err
    assert "uv" in err

    # hook.log carries the same facts for forensics.
    logged = _events(suite, temp_home, "host_python_too_old_for_venv")
    assert logged, f"{suite.name}: no host_python_too_old_for_venv event in hook.log"
    assert logged[-1]["python"] == sys.executable
    assert logged[-1]["version"] == "3.9.6"
    assert logged[-1]["required"] == "3.10"

    # And the marker the next SessionStart reads (shared root: the venv is shared).
    marker = _marker(temp_home)
    assert marker.exists(), f"{suite.name}: {marker} not written"
    recorded = json.loads(marker.read_text(encoding="utf-8"))
    assert recorded["python"] == sys.executable
    assert "Python 3.10 or newer" in recorded["message"]

    # A refusal must not leave a venv-ready marker behind.
    assert not ss._VENV_READY_MARKER.exists()


def test_fallback_proceeds_on_310_host(suite, ss, temp_home, monkeypatch):
    """The gate is specific to < 3.10: a 3.10 host reaches the venv build."""
    monkeypatch.setattr(sys, "version_info", (3, 10, 0, "final", 0))
    calls: list[list[str]] = []

    def _record(cmd, *args, **kwargs):
        calls.append([str(c) for c in cmd])
        raise OSError("hermetic: no real venv build")  # the install then fails cleanly

    monkeypatch.setattr(ss.subprocess, "run", _record)

    assert ss.ensure_cognee_installed() is False  # no venv came out of the fake build
    assert calls, f"{suite.name}: 3.10 host never reached the venv build"
    assert calls[0][:3] == [sys.executable, "-m", "venv"]
    assert not _marker(temp_home).exists()
    assert not _events(suite, temp_home, "host_python_too_old_for_venv")


def test_venv_ready_clears_the_marker(suite, ss, temp_home):
    marker = _marker(temp_home)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"message": "stale"}), encoding="utf-8")

    ss._write_venv_ready("1.5.4")

    assert not marker.exists(), f"{suite.name}: marker survived a successful venv build"
    assert ss._VENV_READY_MARKER.exists()


# --- 3. the refusal reaches the user ------------------------------------------


def test_session_start_output_carries_the_warning(suite, ss, temp_home):
    marker = _marker(temp_home)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"message": "Cognee Memory: host python too old"}), encoding="utf-8"
    )

    fresh = ss._apply_host_python_warning({})
    assert fresh["systemMessage"] == "Cognee Memory: host python too old"
    assert fresh["hookSpecificOutput"]["systemMessage"] == "Cognee Memory: host python too old"
    assert fresh["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    # Appended after, never replacing, whatever the hook already had to say.
    existing = {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "systemMessage": "## Connected"}
    }
    merged = ss._apply_host_python_warning(existing)
    assert merged["hookSpecificOutput"]["systemMessage"] == (
        "## Connected\n\nCognee Memory: host python too old"
    )
    assert merged["systemMessage"] == "Cognee Memory: host python too old"
    # The input is not mutated.
    assert existing["hookSpecificOutput"]["systemMessage"] == "## Connected"


def test_session_start_output_untouched_without_marker(suite, ss, temp_home):
    assert not _marker(temp_home).exists()
    original = {"hookSpecificOutput": {"hookEventName": "SessionStart", "systemMessage": "hi"}}
    assert ss._apply_host_python_warning(original) == original
    assert ss._apply_host_python_warning({}) == {}


# --- 4. a real 3.9 interpreter, when the machine has one -----------------------


def _find_python39() -> str:
    """A Python 3.9 on this machine, or '' — the CI cell guarantees coverage."""
    candidates = [shutil.which("python3.9") or ""]
    if sys.platform == "darwin":
        candidates.append("/Library/Developer/CommandLineTools/usr/bin/python3")
    for candidate in candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "import sys; print(sys.version_info[:2] == (3, 9))"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            continue
        if probe.returncode == 0 and probe.stdout.strip() == "True":
            return candidate
    return ""


_PY39 = _find_python39()


@pytest.mark.skipif(not _PY39, reason="no Python 3.9 interpreter on this machine")
@pytest.mark.parametrize("script", ["credits-refresh.py", "clear-transcript-context.py"])
def test_hook_runs_under_real_python39(suite, run_hook, script):
    """The regression as a user saw it: `python3 <hook>` under 3.9.6 exits 0.

    ``clear-transcript-context.py`` is Claude Code only; the other suites skip it.
    Both hooks are cheap and network-free, so this stays hermetic.
    """
    if not (suite.scripts_dir / script).exists():
        pytest.skip(f"{suite.name} has no {script}")
    result = run_hook(suite, script, stdin={}, python=_PY39, timeout=60)
    assert result.returncode == 0, result.stderr
    assert "TypeError" not in result.stderr
    assert "unsupported operand" not in result.stderr
