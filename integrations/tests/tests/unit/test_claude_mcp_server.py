"""The plugin's MCP server does the hooks' work (SDK-1411, Claude Code).

Hooks no longer run the plugin's scripts. hooks/hooks.json holds ``mcp_tool``
hooks that call the server's ``cognee_hook`` tool, and two relay commands
(``hook-relay.py``) for the events where Claude Code cannot reach a server.
The server runs the scripts listed in scripts/server-hooks.json. These tests
cover:

- the wiring: every event the server has scripts for is reachable from
  hooks.json, and the manifest hands every option to the server;
- the server: the MCP handshake, payload reconstruction, matcher filtering,
  and that a script gets the payload on stdin and its output is returned;
- the relay: a request file is served and its response printed, and a missing
  server is reported once the grace period is over;
- the relay and the hook runner name no key and import nothing from the plugin.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[3] / "claude-code"
_SCRIPTS = _PLUGIN / "scripts"
_SERVER = _SCRIPTS / "mcp_server.py"
_RELAY = _SCRIPTS / "hook-relay.py"
_SERVER_REF = "plugin:cognee-memory:cognee"
#: The events Claude Code cannot deliver to a server: at launch, and at exit.
_RELAYED = {"SessionStart", "SessionEnd"}


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("COGNEE_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    return _load(_SERVER, "cognee_mcp_server_under_test")


@pytest.fixture
def relay(monkeypatch, tmp_path):
    monkeypatch.setenv("COGNEE_PLUGIN_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    return _load(_RELAY, "cognee_hook_relay_under_test")


def _hooks() -> dict:
    return json.loads((_PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]


def _table() -> dict:
    return json.loads((_SCRIPTS / "server-hooks.json").read_text(encoding="utf-8"))["hooks"]


def _manifest() -> dict:
    return json.loads((_PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))


# -------------------------------------------------------------------- wiring


def test_every_server_event_is_reachable_from_hooks_json():
    hooks, table = _hooks(), _table()
    assert set(hooks) == set(table)
    for event, groups in hooks.items():
        entries = [hook for group in groups for hook in group["hooks"]]
        assert len(entries) == 1, f"{event}: one hook hands the event to the server"
        hook = entries[0]
        if event in _RELAYED:
            assert hook["type"] == "command" and "hook-relay.py" in hook["command"]
        else:
            assert hook["type"] == "mcp_tool"
            assert hook["server"] == _SERVER_REF and hook["tool"] == "cognee_hook"
            assert hook["input"]["hook_event_name"] == "${hook_event_name}"
            assert hook["input"]["session_id"] == "${session_id}"


def test_hooks_json_keeps_each_matcher_of_the_server_table():
    hooks, table = _hooks(), _table()
    for event in table:
        assert [g.get("matcher") for g in hooks[event]] == [g.get("matcher") for g in table[event]]


def test_no_hook_command_runs_a_script_that_talks_to_cognee():
    """The only scripts Claude Code still runs as hooks are the runner and the relay."""
    for groups in _hooks().values():
        for group in groups:
            for hook in group["hooks"]:
                command = hook.get("command", "")
                names = {part.rsplit("/", 1)[-1].strip('"') for part in command.split()}
                scripts = {name for name in names if name.endswith(".py")}
                assert scripts <= {"hook_runner.py", "hook-relay.py"}


def test_session_end_relay_does_not_wait():
    command = _hooks()["SessionEnd"][0]["hooks"][0]["command"]
    assert "--no-wait" in command


def test_the_manifest_starts_the_server_with_every_option():
    manifest = _manifest()
    entry = manifest["mcpServers"]["cognee"]
    assert entry["command"] == "python3"
    assert entry["args"] == ["${CLAUDE_PLUGIN_ROOT}/scripts/mcp_server.py"]
    expected = {
        "CLAUDE_PLUGIN_OPTION_" + name.upper(): "${user_config.%s}" % name
        for name in manifest["userConfig"]
    }
    assert entry["env"] == expected


def test_each_event_has_at_most_one_script_whose_output_is_returned(server):
    """One tool result carries one hook output, so only one script per event may answer."""
    table = server.load_hook_table()
    for event in table:
        for match in ("Read", "Bash", "startup"):
            payload = {"tool_name": match, "source": match}
            answering = [e for e in server.entries_for(event, payload, table) if not e["async"]]
            assert len(answering) <= 1, (event, match, answering)


# -------------------------------------------------------------------- server


def test_parse_command_reads_the_script_and_its_arguments(server):
    for groups in _table().values():
        for group in groups:
            for hook in group["hooks"]:
                script, args = server.parse_command(hook["command"])
                assert (_SCRIPTS / script).is_file()
                assert all(arg.startswith("--") for arg in args)
    stop = [server.parse_command(h["command"]) for h in _table()["Stop"][0]["hooks"]]
    assert ("store-to-session.py", ["--stop"]) in stop


@pytest.mark.parametrize(
    "command",
    [
        'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session-start.py"',
        'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hook_runner.py"',
        'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hook_runner.py" "/etc/passwd"',
        'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hook_runner.py" "../../outside.py"',
    ],
)
def test_parse_command_refuses_anything_but_a_plugin_script(server, command):
    with pytest.raises(ValueError):
        server.parse_command(command)


def test_matcher_filters_entries_like_claude_code(server):
    table = server.load_hook_table()
    read = server.entries_for("PreToolUse", {"tool_name": "Read"}, table)
    assert [e["script"] for e in read] == ["file-context.py"]
    assert server.entries_for("PreToolUse", {"tool_name": "Bash"}, table) == []
    assert server.entries_for("PostToolUse", {"tool_name": "WebFetch"}, table) == []
    assert server.entries_for("NoSuchEvent", {}, table) == []


def test_payload_is_rebuilt_from_substituted_input(server):
    payload = server.payload_from_arguments(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "s1",
            "agent_id": "",
            "tool_input": '{"command": "ls"}',
            "tool_response": "plain text, not JSON",
            "stop_hook_active": "true",
            "prompt": '{"looks": "like json"}',
        }
    )
    assert payload == {
        "hook_event_name": "PostToolUse",
        "session_id": "s1",
        "tool_input": {"command": "ls"},
        "tool_response": "plain text, not JSON",
        "stop_hook_active": True,
        "prompt": '{"looks": "like json"}',
    }


@pytest.fixture
def fake_plugin(tmp_path, monkeypatch):
    """A copy of the server beside two tiny scripts and its own hook table."""
    scripts = tmp_path / "plugin" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("mcp_server.py", "hook_runner.py", "hook-relay.py"):
        (scripts / name).write_text((_SCRIPTS / name).read_text(encoding="utf-8"), encoding="utf-8")
    (scripts / "answer.py").write_text(
        "import json, os, sys\n"
        "payload = json.load(sys.stdin)\n"
        "print(json.dumps({'echo': payload, 'args': sys.argv[1:],\n"
        "                  'session_env': os.environ.get('CLAUDE_CODE_SESSION_ID', ''),\n"
        "                  'option': os.environ.get(\n"
        "                      'CLAUDE_PLUGIN_OPTION_COGNEE_BASE_URL', '')}))\n",
        encoding="utf-8",
    )
    (scripts / "background.py").write_text(
        "import json, os, sys\n"
        "payload = json.load(sys.stdin)\n"
        "open(os.environ['MARKER'], 'w').write(payload['hook_event_name'])\n",
        encoding="utf-8",
    )

    def command(script, *args):
        root = "${CLAUDE_PLUGIN_ROOT}/scripts/"
        return 'python3 "%shook_runner.py" "%s%s"%s' % (
            root,
            root,
            script,
            "".join(" " + a for a in args),
        )

    table = {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {"type": "command", "command": command("answer.py", "--flag")},
                        {"type": "command", "command": command("background.py"), "async": True},
                    ]
                }
            ],
            "SessionStart": [{"hooks": [{"type": "command", "command": command("answer.py")}]}],
            "SessionEnd": [{"hooks": [{"type": "command", "command": command("background.py")}]}],
        }
    }
    (scripts / "server-hooks.json").write_text(json.dumps(table), encoding="utf-8")
    state = tmp_path / "state"
    marker = tmp_path / "marker.txt"
    env = {
        **os.environ,
        "COGNEE_PLUGIN_STATE_DIR": str(state),
        "HOME": str(tmp_path / "home"),
        "MARKER": str(marker),
        "CLAUDE_PLUGIN_OPTION_COGNEE_BASE_URL": "https://example.invalid",
        "CLAUDE_CODE_SESSION_ID": "boot-session",
    }
    env.pop("CLAUDE_PID", None)
    return {"scripts": scripts, "state": state, "marker": marker, "env": env}


class _Client:
    def __init__(self, fake):
        self.process = subprocess.Popen(
            [sys.executable, str(fake["scripts"] / "mcp_server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=fake["env"],
            text=True,
            encoding="utf-8",
        )
        self._next = 0

    def request(self, method, params=None):
        self._next += 1
        message = {"jsonrpc": "2.0", "id": self._next, "method": method, "params": params or {}}
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        while True:
            reply = json.loads(self.process.stdout.readline())
            if reply.get("id") == self._next:
                return reply

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=20)


def _wait_for(predicate, seconds=20.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_server_speaks_mcp_and_runs_the_scripts_for_an_event(fake_plugin):
    client = _Client(fake_plugin)
    try:
        hello = client.request("initialize", {"protocolVersion": "2025-06-18"})
        assert hello["result"]["protocolVersion"] == "2025-06-18"
        assert hello["result"]["capabilities"] == {"tools": {}}
        tools = client.request("tools/list")["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["cognee_hook"]
        assert client.request("ping")["result"] == {}
        assert client.request("resources/list")["error"]["code"] == -32601

        reply = client.request(
            "tools/call",
            {
                "name": "cognee_hook",
                "arguments": {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "after-clear",
                    "prompt": "hello",
                    "agent_id": "",
                },
            },
        )
        output = json.loads(reply["result"]["content"][0]["text"])
        assert output["echo"] == {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "after-clear",
            "prompt": "hello",
        }
        assert output["args"] == ["--flag"]
        # The script sees the event's session, not the one the server started in,
        # and the options the manifest gave the server.
        assert output["session_env"] == "after-clear"
        assert output["option"] == "https://example.invalid"
        assert _wait_for(fake_plugin["marker"].exists), "the async script did not run"
        assert fake_plugin["marker"].read_text() == "UserPromptSubmit"

        unknown = client.request("tools/call", {"name": "nope", "arguments": {}})
        assert unknown["result"]["isError"] is True
        empty = client.request(
            "tools/call", {"name": "cognee_hook", "arguments": {"hook_event_name": "Stop"}}
        )
        assert empty["result"]["content"][0]["text"] == ""
    finally:
        client.close()


def _relay_env(fake, host_pid):
    return {**fake["env"], "CLAUDE_PID": str(host_pid)}


def test_relay_request_is_served_and_its_output_printed(fake_plugin):
    client = _Client(fake_plugin)
    try:
        client.request("initialize", {})
        payload = {
            "hook_event_name": "SessionStart",
            "session_id": "boot-session",
            "source": "startup",
        }
        # The relay sees Claude Code's pid; the server's directory is named
        # after its parent, which here is this test process.
        done = subprocess.run(
            [sys.executable, str(fake_plugin["scripts"] / "hook-relay.py"), "--timeout", "30"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=_relay_env(fake_plugin, os.getpid()),
            timeout=60,
        )
        assert json.loads(done.stdout)["echo"] == payload
    finally:
        client.close()


def test_relay_finds_the_server_by_session_when_the_pid_differs(fake_plugin):
    """A launcher between Claude Code and the server changes the server's parent pid."""
    client = _Client(fake_plugin)
    try:
        client.request("initialize", {})
        payload = {"hook_event_name": "SessionStart", "session_id": "boot-session"}
        done = subprocess.run(
            [sys.executable, str(fake_plugin["scripts"] / "hook-relay.py"), "--timeout", "30"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=_relay_env(fake_plugin, 999_999),
            timeout=60,
        )
        assert json.loads(done.stdout)["echo"] == payload
    finally:
        client.close()


def test_session_end_request_left_after_the_server_stopped_is_still_run(fake_plugin):
    client = _Client(fake_plugin)
    client.request("initialize", {})
    client.close()
    payload = {"hook_event_name": "SessionEnd", "session_id": "boot-session", "reason": "exit"}
    done = subprocess.run(
        [sys.executable, str(fake_plugin["scripts"] / "hook-relay.py"), "--no-wait"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_relay_env(fake_plugin, os.getpid()),
        timeout=30,
    )
    assert done.stdout == ""
    assert _wait_for(fake_plugin["marker"].exists), "the finisher did not run SessionEnd"
    assert fake_plugin["marker"].read_text() == "SessionEnd"


def test_a_restarted_server_does_not_end_the_session(fake_plugin):
    """Claude Code is still running and sent no SessionEnd: nothing is synced."""
    client = _Client(fake_plugin)
    client.request(
        "tools/call",
        {"name": "cognee_hook", "arguments": {"hook_event_name": "Stop", "session_id": "s"}},
    )
    client.close()
    log = fake_plugin["state"] / "claude-code" / "mcp-server.log"
    assert _wait_for(lambda: log.exists() and '"finished"' in log.read_text(), 30)
    assert not fake_plugin["marker"].exists()


# --------------------------------------------------------------------- relay


def test_relay_reports_a_missing_server_after_the_grace_period(relay, monkeypatch):
    monkeypatch.setattr(relay, "_SERVER_GRACE_SECONDS", 0.2)
    output = relay.relay(
        {"hook_event_name": "SessionStart", "session_id": "s"}, wait=True, timeout=5
    )
    assert "MCP server" in json.loads(output)["systemMessage"]


def test_relay_without_wait_and_without_a_server_is_silent(relay):
    payload = {"hook_event_name": "SessionEnd", "session_id": "s"}
    assert relay.relay(payload, wait=False, timeout=5) == ""


def test_relay_ignores_input_that_is_not_a_hook_payload(relay, monkeypatch, capsys):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert relay.main(["hook-relay.py"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("name", ["hook-relay.py", "hook_runner.py", "mcp_server.py"])
def test_the_processes_claude_code_starts_import_nothing_from_the_plugin(name):
    """They must start before the plugin's venv is known, and stay easy to read."""
    plugin_modules = {path.stem for path in _SCRIPTS.glob("*.py")}
    tree = ast.parse((_SCRIPTS / name).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & plugin_modules


@pytest.mark.parametrize("name", ["hook-relay.py", "hook_runner.py"])
def test_hook_processes_name_no_plugin_option_and_no_key(name):
    """What Claude Code runs as a hook reads no option, so it can hold no key."""
    text = (_SCRIPTS / name).read_text(encoding="utf-8")
    assert "CLAUDE_PLUGIN_OPTION" not in text
    for needle in ("API_KEY", "Authorization", "urllib", "http.client", "socket"):
        assert needle not in text, f"{name} mentions {needle}"
