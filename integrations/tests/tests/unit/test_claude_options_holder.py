"""The options holder hands the plugin's options to its hooks (SDK-1620).

Claude Code starts ``scripts/_holder.py`` as a local MCP server with the
plugin's options as arguments, which is the form the plugin directory accepts
for a plugin's keys. The hooks read the options from it (``_options.py``)
instead of from Claude Code's ``CLAUDE_PLUGIN_OPTION_*`` variables.

Pinned here:

* the values leave the command line at once: the holder re-runs itself with
  them on a pipe, so ``ps`` never shows them after start-up;
* a hook finds its own session's holder by Claude Code's pid, and records it
  for the processes it starts; a shell (a skill's command) finds none;
* a process keeps what it read after its holder has gone with its session;
* the loopback-port form (no Unix sockets) answers only with its code;
* the holder removes its files when Claude Code closes it;
* the manifest hands the holder every option, in the holder's order.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from utils.options_holder import holder_env
from utils.suites import CLAUDE

_SCRIPTS = CLAUDE.scripts_dir
_HOLDER = _SCRIPTS / "_holder.py"
_VALUES = ["https://tenant.cognee.test", "ck-s3cr3t-value", "", "", "true", "", "pw-s3cr3t"]

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="Unix socket and command-line hand-over"
)


def _env(home: Path, **extra: str) -> dict:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home)}
    # Windows cannot open a socket in a process without these.
    for name in ("SYSTEMROOT", "SystemRoot", "WINDIR"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    env.update(extra)
    return env


def _start_holder(home: Path) -> subprocess.Popen:
    """Start the holder the way Claude Code does: its options as arguments."""
    return subprocess.Popen(
        [sys.executable, str(_HOLDER), *_VALUES],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(home),
        text=True,
    )


def _record(home: Path, pid: int) -> Path:
    return home / ".cognee-plugin" / "claude-code" / "holder" / ("%d.json" % pid)


def _record_of_holder(home: Path) -> Path:
    """The record the holder writes: ``<pid>.json``, under the pid of the
    process that started it (this test; on Windows a virtualenv's launcher may
    sit in between and is skipped, so the pid is looked up, not assumed)."""
    folder = home / ".cognee-plugin" / "claude-code" / "holder"
    for _ in range(200):
        found = sorted(folder.glob("*.json")) if folder.is_dir() else []
        if found:
            return found[0]
        time.sleep(0.05)
    raise AssertionError("no holder record appeared in %s" % folder)


def _wait_for(path: Path) -> None:
    for _ in range(100):
        if path.is_file():
            return
        time.sleep(0.05)
    raise AssertionError("%s never appeared" % path)


def _read_options(home: Path, **env: str) -> dict:
    code = "import json, _options; print(json.dumps(_options.values()))"
    ran = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_SCRIPTS),
        env=_env(home, **env),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stderr
    return json.loads(ran.stdout.strip().splitlines()[-1])


@pytest.fixture
def holder(tmp_path):
    proc = _start_holder(tmp_path)
    # The holder is keyed by its parent's pid: the parent is this test process,
    # as Claude Code is for a real one.
    record = _record_of_holder(tmp_path)
    yield tmp_path, proc, record
    if proc.poll() is None:
        proc.stdin.close()
        proc.wait(timeout=10)


@_POSIX_ONLY
def test_the_values_leave_the_command_line(holder):
    home, proc, _ = holder
    # -ww: without it ps cuts the line at the terminal width (80 columns in CI),
    # which hid the hand-over and made the secret check pass on its own.
    shown = subprocess.run(
        ["ps", "-ww", "-o", "command=", "-p", str(proc.pid)], capture_output=True, text=True
    ).stdout
    assert "_holder.py --from-fd" in shown
    assert "s3cr3t" not in shown


@_POSIX_ONLY
def test_a_hook_finds_its_sessions_holder_by_claude_codes_pid(checked_holder):
    home, scripts, record = checked_holder
    got = _hook_reads(home, scripts, CLAUDE_PID=record.stem)["mine"]
    assert got["cognee_api_key"] == "ck-s3cr3t-value"
    assert got["cognee_base_url"] == "https://tenant.cognee.test"
    assert got["cognee_user_password"] == "pw-s3cr3t"
    assert got["local_models"] == "true"
    # An unset option arrives empty, as Claude Code passes it.
    assert got["llm_api_key"] == ""


@_POSIX_ONLY
def test_a_hook_finds_the_holder_through_its_ancestors_without_claude_pid(checked_holder):
    home, scripts, _ = checked_holder
    assert _hook_reads(home, scripts)["mine"]["cognee_api_key"] == "ck-s3cr3t-value"


def test_a_shell_finds_no_holder(holder):
    """A skill's command is neither a hook nor started by one: it uses the relay."""
    home, _, record = holder
    assert _read_options(home, CLAUDE_PID=record.stem) == {}


@_POSIX_ONLY
def test_a_hook_records_the_holder_for_what_it_starts(checked_holder):
    home, scripts, record = checked_holder
    got = _hook_reads(home, scripts, "--and-a-child", CLAUDE_PID=record.stem)
    assert got["holder"] == str(record)
    # The child finds the holder through that variable alone.
    assert got["child"]["cognee_api_key"] == "ck-s3cr3t-value"


@_POSIX_ONLY
def test_the_holder_removes_its_files_when_claude_code_closes_it(holder):
    home, proc, record = holder
    sock = record.with_suffix(".sock")
    assert sock.exists()
    assert oct(record.stat().st_mode & 0o777) == "0o600"
    assert oct(record.parent.stat().st_mode & 0o777) == "0o700"
    proc.stdin.close()
    proc.wait(timeout=10)
    assert not record.exists() and not sock.exists()


def test_the_holder_answers_claude_code_as_a_server_with_no_tools(holder):
    _, proc, _ = holder
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n")
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
    proc.stdin.flush()
    first = json.loads(proc.stdout.readline())
    second = json.loads(proc.stdout.readline())
    assert first["result"]["serverInfo"]["name"] == "cognee-options"
    assert second["result"] == {"tools": []}


def _serve(state: Path, values: dict):
    """A holder of the test's own, closed by the test (``holder_env``'s are shared)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_holder_served_by_test", _HOLDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.serve(values, state)[1]


def test_a_process_keeps_what_it_read_after_the_holder_is_gone(tmp_path):
    record = tmp_path / ".cognee-plugin" / "claude-code" / "holder" / "7.json"
    close = _serve(record, {"cognee_api_key": "ck-kept"})
    code = f"""
import json, os, _options
first = _options.value("cognee_api_key")
os.remove({str(record)!r})
print(json.dumps([first, _options.value("cognee_api_key")]))
"""
    try:
        ran = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_SCRIPTS),
            env=_env(tmp_path, COGNEE_OPTIONS_HOLDER=str(record)),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        close()
    assert json.loads(ran.stdout.strip()) == ["ck-kept", "ck-kept"], ran.stderr


_CHILD_KEY = "import json, _options; print(json.dumps(_options.value('cognee_api_key')))"


def test_a_child_started_after_the_holder_is_gone_gets_the_options_on_a_pipe(tmp_path):
    """SDK-1620 review #1: the exit watcher's final sync and the cloud worker's
    improve passes start after their session may have ended."""
    record = tmp_path / ".cognee-plugin" / "claude-code" / "holder" / "8.json"
    close = _serve(record, {"cognee_api_key": "ck-piped"})
    code = f"""
import json, os, subprocess, sys, _options
_options.values()
os.remove({str(record)!r})
child = _options.popen(
    [sys.executable, "-c", {_CHILD_KEY!r}],
    env=dict(os.environ),
    stdout=subprocess.PIPE,
    text=True,
)
out, _ = child.communicate(timeout=30)
print(out.strip().splitlines()[-1])
"""
    try:
        ran = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_SCRIPTS),
            env=_env(tmp_path, COGNEE_OPTIONS_HOLDER=str(record), PYTHONPATH=str(_SCRIPTS)),
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        close()
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout.strip().splitlines()[-1]) == "ck-piped"


def test_without_the_pipe_a_child_of_an_ended_session_has_no_key(tmp_path):
    """What the pipe is for: the inherited holder is gone and nothing else
    names this process's session."""
    record = tmp_path / ".cognee-plugin" / "claude-code" / "holder" / "9.json"
    got = subprocess.run(
        [sys.executable, "-c", _CHILD_KEY],
        cwd=str(_SCRIPTS),
        env=_env(tmp_path, COGNEE_OPTIONS_HOLDER=str(record), CLAUDE_PLUGIN_ROOT="/plugin"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(got.stdout.strip()) == ""


def test_the_relay_runs_each_script_against_its_callers_session(tmp_path, monkeypatch):
    """SDK-1620 review #3 and #4: the relay holds no key, so its fingerprint is
    the same for every session, and each relayed script is pointed at the
    holder of the session that asked."""
    import importlib

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.syspath_prepend(str(_SCRIPTS))
    for name in ("_relay", "_options", "_env_file"):
        sys.modules.pop(name, None)
    relay = importlib.import_module("_relay")
    options = importlib.import_module("_options")

    folder = tmp_path / ".cognee-plugin" / "claude-code" / "holder"
    first, second = folder / "101.json", folder / "202.json"
    close_first = _serve(first, {"cognee_api_key": "ck-one"})
    close_second = _serve(second, {"cognee_api_key": "ck-two"})
    try:
        monkeypatch.setenv("COGNEE_OPTIONS_HOLDER", str(first))
        seen_by_one = relay._fingerprint()
        options._source[0] = ""
        monkeypatch.setenv("COGNEE_OPTIONS_HOLDER", str(second))
        assert relay._fingerprint() == seen_by_one

        assert relay._child_env({}, str(second))["COGNEE_OPTIONS_HOLDER"] == str(second)
        # Only a live record in the holder folder is taken from a caller.
        stray = tmp_path / "elsewhere.json"
        stray.write_text("{}", encoding="utf-8")
        assert relay._child_env({}, str(stray))["COGNEE_OPTIONS_HOLDER"] == str(second)
        assert relay._child_env({}, str(folder / "303.json"))["COGNEE_OPTIONS_HOLDER"] == str(
            second
        )
    finally:
        close_first()
        close_second()


def test_a_forced_local_backend_still_wins_after_the_options_arrive(tmp_path):
    """SDK-1620 review #8: session start applies the URL option after waiting
    for the holder; the forced-local switch must be applied after it again."""
    record = tmp_path / ".cognee-plugin" / "claude-code" / "holder" / "11.json"
    close = _serve(record, {"cognee_base_url": "https://tenant.cognee.test"})
    code = (
        "import json, os, _env_file; _env_file.load_env_file();"
        "_env_file.wait_for_options(2.0);"
        "print(json.dumps(os.environ.get('COGNEE_BASE_URL')))"
    )
    try:
        ran = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(_SCRIPTS),
            env=_env(
                tmp_path,
                COGNEE_OPTIONS_HOLDER=str(record),
                CLAUDE_PLUGIN_ROOT="/plugin",
                COGNEE_BACKEND="local",
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        close()
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout.strip().splitlines()[-1]) == ""


def test_the_relay_can_withhold_the_key_for_another_server(tmp_path):
    env = holder_env({"cognee_api_key": "ck-x", "llm_api_key": "sk-y"})
    code = (
        "import json, _options;"
        "print(json.dumps([_options.value('cognee_api_key'), _options.value('llm_api_key')]))"
    )
    ran = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_SCRIPTS),
        env=_env(tmp_path, COGNEE_RELAY_OTHER_SERVER="1", **env),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(ran.stdout.strip()) == ["", "sk-y"]


def test_the_loopback_form_answers_only_with_its_code(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("_holder_for_port_test", _HOLDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / "holder" / "1.json"
    _, cleanup = module.serve({"cognee_api_key": "ck-port"}, state, port_mode=True)
    try:
        record = json.loads(state.read_text(encoding="utf-8"))
        with socket.create_connection(("127.0.0.1", record["port"]), timeout=5) as conn:
            conn.sendall(b"not-the-code\n")
            assert conn.recv(4096) == b""
        got = _read_options(tmp_path, COGNEE_OPTIONS_HOLDER=str(state))
        assert got["cognee_api_key"] == "ck-port"
    finally:
        cleanup()


def test_the_manifest_hands_the_holder_every_option_in_its_order():
    import importlib.util

    manifest = json.loads(CLAUDE.plugin_manifest.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("_holder_for_manifest_test", _HOLDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = manifest["mcpServers"]["options"]
    assert server["command"] == "python3"
    assert server["args"][0] == "${CLAUDE_PLUGIN_ROOT}/scripts/_holder.py"
    assert server["args"][1:] == ["${user_config.%s}" % name for name in module.NAMES]
    assert set(module.NAMES) <= set(manifest["userConfig"])
    for name, option in manifest["userConfig"].items():
        if option.get("sensitive"):
            assert name in module.NAMES, name


def test_no_script_reads_claude_codes_option_variables():
    """The finding this design clears: no plugin file reads an option variable."""
    found = []
    for path in sorted(_SCRIPTS.glob("*")):
        if path.suffix in (".py", ".sh") and "CLAUDE_PLUGIN_OPTION_" in path.read_text(
            encoding="utf-8"
        ):
            found.append(path.name)
    assert found == []


# ------------------------------------------------- who the holder answers (#4)

_PROBE = """
import json, os, subprocess, sys
import _options
mine = _options.values()
child = {}
if len(sys.argv) > 1 and sys.argv[1] == "--and-a-child":
    out = subprocess.run(
        [sys.executable, "-c", "import json, _options; print(json.dumps(_options.values()))"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True, text=True, timeout=30,
    ).stdout
    child = json.loads(out.strip().splitlines()[-1])
holder = os.environ.get("COGNEE_OPTIONS_HOLDER", "")
print(json.dumps({"mine": mine, "child": child, "holder": holder}))
"""


@pytest.fixture
def checked_holder(tmp_path):
    """A holder started the way Claude Code starts it, from a copy of the
    plugin's scripts that also holds a probe script, so the holder's caller
    check runs against real command lines."""
    import shutil

    scripts = tmp_path / "plugin" / "scripts"
    shutil.copytree(_SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    (scripts / "probe_options.py").write_text(_PROBE, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    proc = subprocess.Popen(
        [sys.executable, str(scripts / "_holder.py"), *_VALUES],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(home),
        text=True,
    )
    record = _record_of_holder(home)
    yield home, scripts, record
    proc.stdin.close()
    proc.wait(timeout=10)


def _ask_as(home: Path, scripts: Path, record: Path, argv: list) -> dict:
    ran = subprocess.run(
        [sys.executable, *argv],
        cwd=str(scripts),
        env=_env(home, COGNEE_OPTIONS_HOLDER=str(record), CLAUDE_PLUGIN_ROOT=str(scripts.parent)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ran.returncode == 0, ran.stderr
    return json.loads(ran.stdout.strip().splitlines()[-1])


def _hook_reads(home: Path, scripts: Path, *args: str, **env: str) -> dict:
    """The probe run as Claude Code runs a hook; it finds the holder itself."""
    ran = subprocess.run(
        [
            sys.executable,
            "-I",
            str(scripts / "hook_runner.py"),
            str(scripts / "probe_options.py"),
            *args,
        ],
        cwd=str(scripts),
        env=_env(home, CLAUDE_PLUGIN_ROOT=str(scripts.parent), **env),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ran.returncode == 0, ran.stderr
    return json.loads(ran.stdout.strip().splitlines()[-1])


@_POSIX_ONLY
def test_the_holder_answers_a_hook_and_what_it_starts(checked_holder):
    home, scripts, record = checked_holder
    got = _ask_as(
        home,
        scripts,
        record,
        ["-I", str(scripts / "hook_runner.py"), str(scripts / "probe_options.py"), "--and-a-child"],
    )
    assert got["mine"]["cognee_api_key"] == "ck-s3cr3t-value"
    assert got["child"]["cognee_api_key"] == "ck-s3cr3t-value"


@_POSIX_ONLY
def test_the_holder_refuses_a_command_run_from_a_shell(checked_holder):
    """A Bash tool command: python -c, or a plugin script started directly."""
    home, scripts, record = checked_holder
    code = "import json, _options; print(json.dumps(_options.values()))"
    assert _ask_as(home, scripts, record, ["-c", code]) == {}
    assert _ask_as(home, scripts, record, [str(scripts / "probe_options.py")])["mine"] == {}


@_POSIX_ONLY
def test_the_holder_refuses_a_hook_runner_without_isolation_or_a_foreign_script(
    checked_holder, tmp_path
):
    home, scripts, record = checked_holder
    runner = str(scripts / "hook_runner.py")
    # Without -I, PYTHONPATH and the like could load code of the caller's own.
    assert _ask_as(home, scripts, record, [runner, str(scripts / "probe_options.py")])["mine"] == {}
    # hook_runner runs any script it is given: only the plugin's own count.
    foreign = tmp_path / "elsewhere" / "probe_options.py"
    foreign.parent.mkdir()
    foreign.write_text(
        "import sys; sys.path.insert(0, %r)\n" % str(scripts) + _PROBE, encoding="utf-8"
    )
    assert _ask_as(home, scripts, record, ["-I", runner, str(foreign)])["mine"] == {}


def test_the_entry_points_the_holder_accepts():
    import importlib.util

    spec = importlib.util.spec_from_file_location("_holder_rules", _HOLDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    s = str(_SCRIPTS)
    ok = module.entry_point
    assert ok(["py", "-I", s + "/hook_runner.py", s + "/session-start.py"])
    assert ok(["py", "-I", s + "/hook_runner.py", s + "/store-to-session.py", "--stop"])
    assert ok(["py", "-I", s + "/_relay.py", "serve"])
    assert not ok(["py", s + "/hook_runner.py", s + "/session-start.py"])
    assert not ok(["py", "-I", s + "/hook_runner.py", "/tmp/evil.py"])
    assert not ok(["py", "-I", s + "/_relay.py", "run", "doctor.py"])
    assert not ok(["py", "-I", s + "/doctor.py", "x"])
    assert not ok(["py", "-c", "print(1)", "x"])


def test_every_hook_and_the_relay_start_under_isolation():
    """The holder answers hooks and the relay only when they run under -I."""
    hooks = json.loads((_SCRIPTS.parent / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for entries in hooks["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
    ]
    assert commands
    for command in commands:
        assert command.startswith('python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/hook_runner.py"')
        assert '|| python -I "${CLAUDE_PLUGIN_ROOT}/scripts/hook_runner.py"' in command
    relay = (_SCRIPTS / "_relay.py").read_text(encoding="utf-8")
    assert '[sys.executable, "-I", str(Path(__file__).resolve()), "serve"]' in relay
