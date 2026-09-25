"""Hooks run through hook_runner.py, launched the way each host launches them.

Every other tier runs a hook as ``[sys.executable, script]`` with a hand-built
environment and PYTHONIOENCODING pinned. The hosts do neither: they substitute
the plugin root into the hooks.json command string and hand it to their shell
(see utils.host_launch for exactly how). On Windows every Codex hook exited 1
with nothing in hook.log, because the failure happened before any hook's own
error handling.

Covered here, for both plugins:

* hook_runner.py itself: a crash (including an import-time one) exits 0, lands
  in hook-crash.log with its traceback and surfaces a rate-limited
  systemMessage; an explicit ``sys.exit`` is left alone; stdio is UTF-8.
* hooks.json as the host executes it, against stub scripts: the quoting, the
  argument passing, that no hook runs twice, and which interpreter each
  real-world Python setup ends up with (py launcher only, python only, the
  Microsoft Store aliases, no Python at all).
* Claude Code on a Windows machine without Git Bash, where it falls back to
  PowerShell.

The real hook scripts, in a whole session, are driven in e2e/test_host_session.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from utils import host_launch as hl
from utils.suites import ALL_SUITES

_RUNNER = hl.RUNNER


@pytest.fixture
def host(suite):
    if not hl.launches_through_runner(suite):
        pytest.skip(f"{suite.name}: hooks are not launched through hook_runner.py")
    return suite


@pytest.fixture
def host_shell(host):
    if not hl.host_shell_available(host):
        pytest.skip(f"{host.name}: the host's shell is not installed")
    return host


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COGNEE_", "PYTHONIO"))}
    env.pop("PYTHONUTF8", None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env.update(extra)
    return env


def _run_runner(suite, home: Path, script: Path, *args: str, stdin: str = "{}"):
    return subprocess.run(
        [sys.executable, str(suite.scripts_dir / _RUNNER), str(script), *args],
        input=stdin.encode("utf-8"),
        env=_env(home),
        capture_output=True,
        timeout=60,
    )


def _crash_log(suite, home: Path) -> Path:
    return home / ".cognee-plugin" / suite.state_subdir / "hook-crash.log"


def test_runner_copies_differ_only_in_the_state_dir():
    """Both plugins ship the runner; a fix to one copy must reach the other."""
    by_name = {s.name: s for s in ALL_SUITES}
    texts = {}
    for name in hl.PLACEHOLDERS:
        path = by_name[name].scripts_dir / _RUNNER
        texts[name] = path.read_text(encoding="utf-8").replace(
            f'_STATE_SUBDIR = "{by_name[name].state_subdir}"', '_STATE_SUBDIR = "<plugin>"'
        )
    assert len(set(texts.values())) == 1, "hook_runner.py copies have drifted apart"


# --- the runner --------------------------------------------------------------


def test_import_time_crash_is_logged_reported_once_and_exits_zero(host, temp_home, tmp_path):
    script = tmp_path / "broken-hook.py"
    script.write_text("import _no_such_plugin_module\n", encoding="utf-8")

    first = _run_runner(host, temp_home, script)
    assert first.returncode == 0, first.stderr
    message = json.loads(first.stdout.decode("utf-8"))["systemMessage"]
    assert "broken-hook.py" in message and "ModuleNotFoundError" in message
    assert "hook-crash.log" in message
    assert b"ModuleNotFoundError" in first.stderr

    log = _crash_log(host, temp_home).read_text(encoding="utf-8")
    assert "Traceback" in log and "_no_such_plugin_module" in log

    # PostToolUse fires on every tool call: the same crash must not repeat the
    # message each time, but it is still logged.
    second = _run_runner(host, temp_home, script)
    assert second.returncode == 0
    assert second.stdout == b""
    assert _crash_log(host, temp_home).read_text(encoding="utf-8").count("Traceback") == 2


def test_crash_after_output_does_not_append_a_second_json_document(host, temp_home, tmp_path):
    script = tmp_path / "half-done.py"
    script.write_text(
        'import json\nprint(json.dumps({"ok": True}))\nraise RuntimeError("late")\n',
        encoding="utf-8",
    )
    result = _run_runner(host, temp_home, script)
    assert result.returncode == 0
    assert json.loads(result.stdout.decode("utf-8")) == {"ok": True}
    assert "RuntimeError: late" in _crash_log(host, temp_home).read_text(encoding="utf-8")


def test_explicit_exit_status_is_preserved(host, temp_home, tmp_path):
    script = tmp_path / "exits.py"
    script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    result = _run_runner(host, temp_home, script)
    assert result.returncode == 3
    assert not _crash_log(host, temp_home).exists()


def test_script_runs_as_main_with_its_own_argv_and_utf8_stdio(host, temp_home, tmp_path):
    script = tmp_path / "echo-hook.py"
    script.write_text(
        "import json, os, sys\n"
        "payload = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'name': __name__, 'argv': sys.argv,\n"
        "                  'file': os.path.basename(__file__),\n"
        "                  'prompt': payload['prompt'],\n"
        "                  'runner': os.environ.get('COGNEE_HOOK_RUNNER', '')},\n"
        "                 ensure_ascii=False))\n",
        encoding="utf-8",
    )
    prompt = "naïve — café ✓ 记忆"
    result = _run_runner(
        host, temp_home, script, "--stop", stdin=json.dumps({"prompt": prompt}, ensure_ascii=False)
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.decode("utf-8"))
    assert out["name"] == "__main__"
    assert out["file"] == "echo-hook.py"
    assert out["argv"] == [str(script), "--stop"]
    assert out["prompt"] == prompt
    assert Path(out["runner"]).name == _RUNNER


# --- hooks.json, executed the way the host executes it -----------------------


def test_every_hook_goes_through_the_runner(host):
    pattern = hl.script_pattern(host)
    keys = ("command", "commandWindows") if host.name == "codex" else ("command",)
    for event, hook in hl.all_hook_entries(host):
        for key in keys:
            command = hook.get(key, "")
            assert command, f"{event}: no {key}"
            scripts = pattern.findall(command)
            assert scripts and scripts[0] == _RUNNER, (
                f"{event} {key} bypasses the runner: {command}"
            )
            assert len(scripts) >= 2 and scripts[1] != _RUNNER, (
                f"{event} {key}: runner without a hook"
            )
        if "commandWindows" in keys:
            # Both variants must launch the same script with the same arguments.
            posix_tail = hook["command"].split(" || ")[0].split(" ", 1)[1]
            windows_tail = hook["commandWindows"].split(" || ")[0].split(" ", 2)[2]
            assert posix_tail == windows_tail, event


_STUB = (
    "import json, os, sys, uuid\n"
    "record = {'script': os.path.basename(__file__), 'args': sys.argv[1:],\n"
    "          'stdin': sys.stdin.read(),\n"
    "          'interpreter': os.environ.get('COGNEE_TEST_INTERPRETER', '')}\n"
    "out = os.path.join(os.environ['STUB_CALLS'], uuid.uuid4().hex + '.json')\n"
    "with open(out, 'w', encoding='utf-8') as f:\n"
    "    json.dump(record, f, ensure_ascii=False)\n"
)


def _stub_plugin_root(suite, tmp_path: Path) -> tuple[Path, Path]:
    """A plugin root whose hook scripts only record how they were invoked."""
    root = tmp_path / "Plugin Root With Spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(suite.scripts_dir / _RUNNER, scripts / _RUNNER)
    calls = tmp_path / "calls"
    calls.mkdir()
    pattern = hl.script_pattern(suite)
    for _, hook in hl.all_hook_entries(suite):
        for name in set(pattern.findall(hook["command"])) - {_RUNNER}:
            (scripts / name).write_text(_STUB, encoding="utf-8")
    return root, calls


def _records(calls: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in calls.iterdir()]


def _run_host(suite, hook, root, home, calls, stdin: bytes, *, path_env=None, login_shell=True):
    env = _env(home, STUB_CALLS=str(calls))
    env.update(hl.host_env(suite, home, root, quiet=False))
    env["STUB_CALLS"] = str(calls)
    if path_env:
        env.update(path_env)
    return subprocess.run(
        hl.host_command(suite, hook, root, login_shell=login_shell),
        input=stdin,
        env=env,
        capture_output=True,
        timeout=60,
    )


def test_hooks_json_commands_run_once_through_the_host_shell(host_shell, temp_home, tmp_path):
    root, calls = _stub_plugin_root(host_shell, tmp_path)
    pattern = hl.script_pattern(host_shell)
    payload = json.dumps({"hook_event_name": "probe", "prompt": "café ✓"}, ensure_ascii=False)

    for event, hook in hl.all_hook_entries(host_shell):
        for stale in calls.iterdir():
            stale.unlink()
        result = _run_host(host_shell, hook, root, temp_home, calls, payload.encode("utf-8"))
        assert result.returncode == 0, f"{event}: {result.stderr.decode('utf-8', 'replace')}"

        records = _records(calls)
        assert len(records) == 1, f"{event}: hook ran {len(records)} times"
        expected_script, *expected_args = (
            pattern.findall(hook["command"])[1:2]
            + hook["command"].split(" || ")[0].rsplit('"', 1)[1].split()
        )
        assert records[0]["script"] == expected_script, event
        assert records[0]["args"] == expected_args, event
        assert records[0]["stdin"] == payload, event


def test_hooks_json_command_survives_a_crashing_hook(host_shell, temp_home, tmp_path):
    root, calls = _stub_plugin_root(host_shell, tmp_path)
    _, hook = hl.all_hook_entries(host_shell)[0]
    target = hl.script_pattern(host_shell).findall(hook["command"])[1]
    (root / "scripts" / target).write_text("raise RuntimeError('boom')\n", encoding="utf-8")

    result = _run_host(host_shell, hook, root, temp_home, calls, b"{}")
    assert result.returncode == 0, result.stderr
    # Exactly one crash: the `||` fallback must not rerun a hook that started.
    log = _crash_log(host_shell, temp_home).read_text(encoding="utf-8")
    assert log.count("RuntimeError: boom") == 1


# --- which Python a real machine ends up with ---------------------------------
#
# (present, Store aliases, interpreter cmd.exe picks, interpreter a POSIX/Git
# Bash shell picks). None means no hook can start: the host must see a failure
# with the reason on stderr rather than a silent success. cmd.exe is Codex on
# Windows (`py -3 … || python …`); the POSIX shell is Codex elsewhere and Claude
# Code everywhere (`python3 … || python …`).
_INTERPRETER_SETUPS = {
    "python.org": (("py", "python"), ("python3",), "py", "python"),
    "python-only": (("python",), (), "python", "python"),
    "store-python": (("python3", "python"), (), "python", "python3"),
    "store-aliases-only": ((), ("python3", "python"), None, None),
    "python3-only": (("python3",), (), None, "python3"),
    "nothing": ((), (), None, None),
}


def _uses_cmd(suite) -> bool:
    return suite.name == "codex" and os.name == "nt"


@pytest.mark.parametrize("setup", list(_INTERPRETER_SETUPS))
def test_each_python_setup_starts_the_hook_once_or_fails_loudly(
    host_shell, temp_home, tmp_path, setup
):
    present, stores, via_cmd, via_sh = _INTERPRETER_SETUPS[setup]
    expected = via_cmd if _uses_cmd(host_shell) else via_sh
    root, calls = _stub_plugin_root(host_shell, tmp_path)
    shims = hl.make_shims(tmp_path / "bin", present=present, store_stubs=stores)
    _, hook = hl.all_hook_entries(host_shell)[0]

    result = _run_host(
        host_shell,
        hook,
        root,
        temp_home,
        calls,
        b"{}",
        path_env=hl.shim_path_env(shims),
        # A login shell may rebuild PATH from the system profile (macOS
        # path_helper), which would put the real interpreters back.
        login_shell=False,
    )
    records = _records(calls)
    stderr = result.stderr.decode("utf-8", "replace")
    if expected is None:
        assert records == [], f"{setup}: a hook ran with no usable Python: {records}"
        assert result.returncode != 0, f"{setup}: no Python, yet the host would see success"
        assert stderr.strip(), f"{setup}: the failure gives the user no reason"
        return
    assert result.returncode == 0, f"{setup}: {stderr}"
    assert [r["interpreter"] for r in records] == [expected], f"{setup}: {records}"


# --- Claude Code on Windows without Git Bash ----------------------------------


def _powershells() -> list:
    windows_ps = shutil.which("powershell.exe")
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    return [
        pytest.param(
            windows_ps,
            id="windows-powershell-5.1",
            marks=[
                pytest.mark.skipif(not windows_ps, reason="Windows PowerShell is not installed"),
                # Windows PowerShell 5.1 has no `||` operator, and every hook
                # command uses it for the python3 -> python fallback.
                pytest.mark.xfail(
                    strict=True,
                    reason="hooks.json's `||` fallback is a parse error in Windows PowerShell 5.1",
                ),
            ],
        ),
        pytest.param(
            pwsh,
            id="powershell-7",
            marks=pytest.mark.skipif(not pwsh, reason="PowerShell 7 is not installed"),
        ),
    ]


@pytest.mark.skipif(os.name != "nt", reason="the PowerShell fallback exists only on Windows")
@pytest.mark.parametrize("powershell", _powershells())
def test_claude_code_hooks_run_under_the_powershell_fallback(
    suite, temp_home, tmp_path, powershell
):
    """Claude Code runs hooks with PowerShell when Git Bash isn't installed."""
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: only Claude Code falls back to PowerShell")
    root, calls = _stub_plugin_root(suite, tmp_path)
    shims = hl.make_shims(tmp_path / "bin", present=("python3", "python"))
    env = _env(temp_home, STUB_CALLS=str(calls), **hl.shim_path_env(shims))
    env["CLAUDE_PLUGIN_ROOT"] = str(root)

    for event, hook in hl.all_hook_entries(suite):
        for stale in calls.iterdir():
            stale.unlink()
        command = hook["command"].replace(hl.PLACEHOLDERS["claude-code"], root.as_posix())
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            input=b"{}",
            env=env,
            capture_output=True,
            timeout=60,
        )
        assert result.returncode == 0, f"{event}: {result.stderr.decode('utf-8', 'replace')}"
        assert len(_records(calls)) == 1, event
