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
    record = _record(tmp_path, os.getpid())
    _wait_for(record)
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


def test_a_hook_finds_its_sessions_holder_by_claude_codes_pid(holder):
    home, _, record = holder
    got = _read_options(home, CLAUDE_PLUGIN_ROOT="/plugin", CLAUDE_PID=str(os.getpid()))
    assert got["cognee_api_key"] == "ck-s3cr3t-value"
    assert got["cognee_base_url"] == "https://tenant.cognee.test"
    assert got["cognee_user_password"] == "pw-s3cr3t"
    assert got["local_models"] == "true"
    # An unset option arrives empty, as Claude Code passes it.
    assert got["llm_api_key"] == ""


def test_a_hook_finds_the_holder_through_its_ancestors_without_claude_pid(holder):
    home, _, _ = holder
    got = _read_options(home, CLAUDE_PLUGIN_ROOT="/plugin")
    assert got["cognee_api_key"] == "ck-s3cr3t-value"


def test_a_shell_finds_no_holder(holder):
    """A skill's command is neither a hook nor started by one: it uses the relay."""
    home, _, _ = holder
    assert _read_options(home, CLAUDE_PID=str(os.getpid())) == {}


def test_a_hook_records_the_holder_for_what_it_starts(holder):
    home, _, record = holder
    code = (
        "import json, os, _options; _options.values();"
        "print(json.dumps(os.environ.get('COGNEE_OPTIONS_HOLDER', '')))"
    )
    ran = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_SCRIPTS),
        env=_env(home, CLAUDE_PLUGIN_ROOT="/plugin", CLAUDE_PID=str(os.getpid())),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(ran.stdout.strip()) == str(record)
    # A child with only that variable (no hook markers) reads the same values.
    assert _read_options(home, COGNEE_OPTIONS_HOLDER=str(record))["cognee_api_key"] == (
        "ck-s3cr3t-value"
    )


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
