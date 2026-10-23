"""Capture controls protect both online and buffered automatic memory."""

import asyncio
import json
from unittest.mock import Mock

import pytest


@pytest.fixture
def policy(suite, isolated_modules):
    return isolated_modules(suite, "_capture_policy")


@pytest.mark.parametrize(
    "path", ["/project/.env", "C:\\repo\\.env.prod", ".ssh/id_rsa", "cert.pem", ".aws/credentials"]
)
def test_default_sensitive_paths_are_excluded(policy, path):
    assert not policy.allow_tool("Read", {"file_path": path})
    assert policy.allow_tool("Read", {"file_path": "/project/main.py"})


def test_tool_allowlist_and_extra_paths(policy, monkeypatch):
    monkeypatch.setenv("COGNEE_CAPTURE_TOOLS", "Grep|Glob")
    assert not policy.allow_tool("Read", {"path": "main.py"})
    assert policy.allow_tool("Grep", {"path": "main.py"})
    monkeypatch.setenv("COGNEE_CAPTURE_DENY_PATHS", '["*.private"]')
    assert not policy.allow_tool("Grep", {"path": "payments.private"})


@pytest.mark.parametrize(
    "text,secret",
    [
        ("password=supersecret", "supersecret"),
        ('{"api_key": "hidden-value"}', "hidden-value"),
        ("Authorization: Bearer abc.secret.value", "abc.secret.value"),
        ("postgresql://user:password@host/db", "user:password"),
        (
            "-----BEGIN PRIVATE KEY-----\nPRIVATE_CONTENT\n-----END PRIVATE KEY-----",
            "PRIVATE_CONTENT",
        ),
        ("ghp_012345678901234567890123456789", "012345678901234567890123456789"),
    ],
)
def test_secret_formats_are_scrubbed(policy, text, secret):
    assert secret not in policy.redact(text)
    assert "[redacted:" in policy.redact(text)


def test_nested_keys_and_custom_patterns(policy, monkeypatch):
    assert policy.redact({"params": {"access_token": "private-value"}}) == {
        "params": {"access_token": "[redacted:credential]"}
    }
    monkeypatch.setenv("COGNEE_CAPTURE_REDACT_PATTERNS", '["customer-[0-9]+"]')
    assert policy.redact("customer-123") == "[redacted:custom]"
    monkeypatch.setenv("COGNEE_CAPTURE_REDACT_PATTERNS", '["["]')
    with pytest.raises(Exception):
        policy.redact("secret-bearing input")


def test_capture_disabled_does_not_touch_session_state(suite, hook_module, monkeypatch):
    prompt = hook_module(suite, "store-user-prompt.py")
    store = hook_module(suite, "store-to-session.py")
    monkeypatch.setenv("COGNEE_CAPTURE", "false")
    for module in (prompt, store):
        monkeypatch.setattr(
            module, "_load_session", Mock(side_effect=AssertionError("capture ran"))
        )
    asyncio.run(prompt._store("a sensitive prompt", {}))
    asyncio.run(store._store_tool_call({"tool_name": "Read", "tool_input": {"path": "normal.py"}}))
    asyncio.run(store._store_assistant_stop({"assistant_message": "a sensitive answer"}))


def test_filtered_trace_never_reaches_session_or_buffer(suite, hook_module, monkeypatch):
    store = hook_module(suite, "store-to-session.py")
    monkeypatch.setattr(store, "_load_session", Mock(side_effect=AssertionError("capture ran")))
    asyncio.run(
        store._store_tool_call(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "/repo/.env"},
                "tool_response": "password=supersecret",
            }
        )
    )


def test_online_and_buffered_trace_receive_redacted_content(suite, hook_module, monkeypatch):
    for online in (False, True):
        store = hook_module(suite, "store-to-session.py")
        if suite.name == "claude-code":
            monkeypatch.setattr(store, "cloud_enabled", lambda *args: False)

        monkeypatch.setattr(store, "_load_session", lambda: ("session", "dataset", "user"))
        monkeypatch.setattr(store, "load_config", lambda: {})
        monkeypatch.setattr(
            store,
            "resolve_runtime_mode",
            lambda: {"mode": "http", "base_url": "http://example.test"},
        )
        monkeypatch.setattr(store, "server_usable", lambda url: online)
        writes = []
        monkeypatch.setattr(
            store, "append_warmup_entry", lambda *args, **kwargs: writes.append(args[2])
        )
        monkeypatch.setattr(
            store, "remember_entry_via_http", lambda *args, **kwargs: writes.append(args[2])
        )
        asyncio.run(
            store._store_tool_call(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo password=supersecret"},
                    "tool_response": "Bearer private.token.value",
                }
            )
        )
        assert len(writes) == 1
        serialized = json.dumps(writes[0])
        assert "supersecret" not in serialized and "private.token.value" not in serialized


@pytest.mark.parametrize(
    "tools,name,params,expected",
    [
        # Tool(prefix:*) borrows Claude Code's permission spelling (#423).
        ("Bash(git:*)|Read", "Bash", {"command": "git status"}, True),
        ("Bash(git:*)|Read", "Bash", {"command": "rg foo src/"}, False),
        ("Bash(git:*)|Read", "Bash", {"command": "gitx weird"}, False),
        ("Bash(git:*)|Read", "Bash", {"command": "  git\tlog -1"}, True),
        ("Bash(git:*)|Read", "Bash", {"command": "git"}, True),
        ("Bash(git:*)|Read", "Bash", {}, False),
        ("Bash(git:*)|Read", "Read", {"file_path": "main.py"}, True),
        ("Bash(git:*)|Read", "Grep", {"pattern": "x"}, False),
        # A multi-word prefix still ends on a word boundary.
        ("Bash(git status:*)", "Bash", {"command": "git status --short"}, True),
        ("Bash(git status:*)", "Bash", {"command": "git log"}, False),
        # Prefix test, not a shell parser: a compound command matches its first word.
        ("Bash(git:*)", "Bash", {"command": "cd x && git status"}, False),
        # Legacy spellings keep their meaning.
        ("Bash", "Bash", {"command": "rg foo src/"}, True),
        ("B*", "Bash", {"command": "rg foo src/"}, True),
        ('["Bash(git:*)", "Edit"]', "Bash", {"command": "git push"}, True),
        ('["Bash(git:*)", "Edit"]', "Edit", {"file_path": "a.py"}, True),
        ('["Bash(git:*)", "Edit"]', "Bash", {"command": "ls"}, False),
    ],
)
def test_command_prefix_patterns(policy, monkeypatch, tools, name, params, expected):
    monkeypatch.setenv("COGNEE_CAPTURE_TOOLS", tools)
    assert policy.allow_tool(name, params) is expected


def test_deny_tools_apply_after_the_allowlist(policy, monkeypatch):
    monkeypatch.setenv("COGNEE_CAPTURE_DENY_TOOLS", "Bash(rg:*)|Bash(grep:*)|Glob")
    assert policy.allow_tool("Bash", {"command": "git status"})
    assert not policy.allow_tool("Bash", {"command": "  rg foo"})
    assert not policy.allow_tool("Bash", {"command": "grep -r foo ."})
    assert not policy.allow_tool("Glob", {"pattern": "*.py"})
    assert policy.allow_tool("Read", {"file_path": "main.py"})
    # Deny wins over an allowlist entry naming the same call.
    monkeypatch.setenv("COGNEE_CAPTURE_TOOLS", "Bash")
    assert not policy.allow_tool("Bash", {"command": "rg foo"})
    assert policy.allow_tool("Bash", {"command": "pytest -q"})
    # The sensitive-path check still runs after both lists.
    assert not policy.allow_tool("Bash", {"command": "pytest", "file_path": "/repo/.env"})


@pytest.mark.parametrize("pattern", ["Bash(rg)", "Bash()", "Bash(rg:*", "Bash(:*)", "(git:*)"])
def test_malformed_patterns_fail_loudly(policy, monkeypatch, pattern):
    """Every misspelling used to match nothing and switch capture off in silence."""
    assert policy.capture_pattern_error() == ""
    # The bad entry is rejected even when an earlier entry would have matched.
    monkeypatch.setenv("COGNEE_CAPTURE_TOOLS", f"Bash(git:*)|{pattern}")
    with pytest.raises(policy.CapturePatternError):
        policy.allow_tool("Bash", {"command": "git status"})
    assert pattern in policy.capture_pattern_error()
    monkeypatch.delenv("COGNEE_CAPTURE_TOOLS")
    monkeypatch.setenv("COGNEE_CAPTURE_DENY_TOOLS", pattern)
    with pytest.raises(policy.CapturePatternError):
        policy.allow_tool("Read", {"file_path": "a.py"})
    assert pattern in policy.capture_pattern_error()


def test_invalid_pattern_fails_closed_in_the_store_hook(suite, hook_module, monkeypatch):
    store = hook_module(suite, "store-to-session.py")
    monkeypatch.setenv("COGNEE_CAPTURE_TOOLS", "Bash(rg)")
    monkeypatch.setattr(store, "_load_session", Mock(side_effect=AssertionError("capture ran")))
    events = []
    monkeypatch.setattr(store, "hook_log", lambda event, detail=None: events.append(event))
    asyncio.run(
        store._store_tool_call({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    )
    assert "capture_tools_invalid" in events


def test_invalid_pattern_keeps_the_warmup_buffer(suite, isolated_modules, tmp_path, monkeypatch):
    """A typo in the allowlist must not cost traces buffered before it was made."""
    common = isolated_modules(suite, "_plugin_common")
    bridge = tmp_path / "bridge.json"
    monkeypatch.setattr(common, "_bridge_file", lambda sid="": bridge)
    monkeypatch.setattr(common, "_DRAIN_LOCK", tmp_path / "drain.lock")
    monkeypatch.setattr(common, "_BUFFER_LOCK", tmp_path / "buffer.lock")
    events = []
    monkeypatch.setattr(common, "hook_log", lambda ev, detail=None: events.append(ev))
    sent = []
    monkeypatch.setattr(
        common, "remember_entry_via_http", lambda d, s, entry, **k: sent.append(entry) or {}
    )
    entry = {"type": "trace", "origin_function": "Bash", "method_params": {"command": "git"}}
    common.append_warmup_entry("ds", "sid", entry)
    monkeypatch.setenv("COGNEE_CAPTURE_TOOLS", "Bash(rg)")
    assert common.drain_warmup_entries("ds", "sid") == (0, 1)
    assert sent == [] and "capture_tools_invalid" in events
    monkeypatch.setenv("COGNEE_CAPTURE_TOOLS", "Bash(git:*)")
    assert common.drain_warmup_entries("ds", "sid") == (1, 0)
    assert len(sent) == 1


def test_invalid_pattern_is_reported_on_the_next_prompt(suite, isolated_modules, monkeypatch):
    """The store hook runs async with hidden output; the recall hook carries the notice."""
    common = isolated_modules(suite, "_plugin_common")
    assert common.with_base_url_notice({}, "UserPromptSubmit") == {}
    monkeypatch.setenv("COGNEE_CAPTURE_DENY_TOOLS", "Bash(rg)")
    output = common.with_base_url_notice({}, "UserPromptSubmit")
    assert "Bash(rg)" in output["systemMessage"]
    assert "Bash(rg)" in output["hookSpecificOutput"]["additionalContext"]


def test_malformed_json_list_is_reported_with_its_variable_name(policy, monkeypatch):
    monkeypatch.setenv("COGNEE_CAPTURE_TOOLS", "[Bash rg]")
    with pytest.raises(policy.CapturePatternError, match="COGNEE_CAPTURE_TOOLS"):
        policy.allow_tool("Bash", {"command": "git status"})
    assert "COGNEE_CAPTURE_TOOLS" in policy.capture_pattern_error()


@pytest.mark.parametrize(
    "command",
    [
        "cat ~/.aws/credentials",
        "less $HOME/.ssh/id_rsa",
        'head "${HOME}/.npmrc"',
        "source .env.local",
        "grep KEY .env",
        "wc -l < .env",
    ],
)
def test_shell_reads_of_secret_files_are_not_captured(policy, command):
    assert not policy.allow_tool("Bash", {"command": command})
    assert policy.allow_tool("Bash", {"command": "cat README.md"})


def test_aws_and_npm_credential_files_are_scrubbed(policy):
    aws = (
        "[default]\naws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"
    )
    npmrc = "//registry.npmjs.org/:_authToken=npm_abcdefghijklmnopqrstuvwxyz0123456789\n"
    scrubbed = policy.redact(aws + npmrc)
    for secret in ("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI", "npm_abcdef"):
        assert secret not in scrubbed


def test_a_glob_python_cannot_compile_is_a_pattern_error_not_a_crash(policy, monkeypatch):
    # Python 3.9's fnmatch passes a reversed range such as ``[C-A]`` to ``re``,
    # which raises re.error; 3.10+ make it match nothing. Simulate the 3.9 error
    # so every interpreter checks it surfaces as the usual pattern error, for tool
    # patterns and for deny-path globs alike.
    import re

    def broken(name, pattern):
        raise re.error("bad character range C-A at position 5")

    monkeypatch.setattr(policy.fnmatch, "fnmatchcase", broken)
    monkeypatch.setenv("COGNEE_CAPTURE_TOOLS", '["[C-A]*"]')
    with pytest.raises(policy.CapturePatternError, match="not a valid glob"):
        policy.allow_tool("Bash", {"command": "ls"})
    monkeypatch.delenv("COGNEE_CAPTURE_TOOLS")
    with pytest.raises(policy.CapturePatternError, match="not a valid glob"):
        policy.allow_tool("Read", {"file_path": "/project/main.py"})


@pytest.mark.parametrize(
    "command,reason",
    [
        (
            '~/.claude/plugins/cache/cognee/cognee-memory/1/scripts/cognee-search.sh "q"',
            "plugin_script",
        ),
        ("cognee-cli recall q", "cognee_cli"),
        ("uv run cognee-cli recall q", "cognee_cli"),
        ('python3 "$CLAUDE_PLUGIN_ROOT/scripts/custom.py"', "plugin_path"),
        ("python3 ~/.cognee-plugin/custom.py", "plugin_path"),
        ("cd ~/cognee/cognee && uv run pytest -x", ""),
        ('python3 -c "import cognee"', ""),
        ("grep -rn cognee src/", ""),
        ("rg cognee-search.sh src/", ""),
        ("cat ~/.cognee-plugin/config.json", ""),
        ('echo "cognee-cli recall"', ""),
        # Codex and Antigravity skills run the plugin from their own roots.
        ('"${CODEX_PLUGIN_ROOT}/scripts/cognee-cli.sh" recall "q"', "plugin_script"),
        ('python3 "${CODEX_PLUGIN_ROOT}/scripts/doctor.py"', "plugin_path"),
        ('python3 "${CODEX_PLUGIN_ROOT}/scripts/sync-session-to-graph.py"', "plugin_script"),
        ('"$CODEX_PLUGIN_ROOT/scripts/cognee-recap.py"', "plugin_script"),
        (
            '"${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}'
            '/scripts/cognee-cli.sh" remember "x"',
            "plugin_script",
        ),
        (
            'python3 "${COGNEE_ANTIGRAVITY_PLUGIN_ROOT:-$HOME/.gemini/config/plugins/cognee}'
            '/scripts/doctor.py"',
            "plugin_path",
        ),
        ("python3 ~/.gemini/config/plugins/cognee/scripts/doctor.py", "plugin_path"),
        ("python3 doctor.py", ""),
    ],
)
def test_plugin_invocation_matches_command_positions(policy, command, reason):
    assert policy.is_plugin_invocation(command) == reason


@pytest.mark.parametrize(
    "command,code,stderr,skip",
    [
        ("cd ~/cognee/cognee && uv run pytest -x", 0, "", False),
        ("cd ~/cognee/cognee && uv run pytest -x", 1, "", False),
        ("cd /private/tmp/claude-501/-Users-x-cognee-cognee && grep -rn foo src/", 0, "", True),
        ("cd /private/tmp/claude-501/-Users-x-cognee-cognee && grep -rn foo src/", 1, "", True),
        ("grep -rn foo src/", 2, "", False),
        ("grep -rn foo src/", 1, "permission denied", False),
        ("export X=y && FOO=bar && ls src/", 0, "", True),
        ("git commit -m cognee", 0, "", False),
        ("git push", 0, "", False),
        ("mkdir cognee", 0, "", False),
        ("rm cognee", 0, "", False),
        ("cat <<'EOF'\ncognee\nEOF", 0, "", False),
        ("for x in cognee; do echo x; done", 0, "", False),
        ("cat x | tee output", 0, "", False),
        ("echo x > file", 0, "", False),
        ("git branch -D cognee", 0, "", False),
        ("git tag release", 0, "", False),
        ("git worktree add ../cognee", 0, "", False),
        ("gh api -X POST repos/x", 0, "", False),
        ("find . -delete", 0, "", False),
        ("sed -i s/a/b/ file", 0, "", False),
        ("git status --short", 0, "", True),
        ("git worktree list", 0, "", True),
        ("gh pr view 42", 0, "", True),
        ("gh api repos/x", 0, "", True),
    ],
)
def test_bash_inspection_keeps_failures_and_mutations(policy, command, code, stderr, skip):
    output = {"stdout": "results", "stderr": stderr, "exit_code": code}
    assert policy.low_signal_bash(command, output, "success" if code == 0 else "error") is skip


@pytest.mark.parametrize(
    "command,code,expected",
    [
        ("cd ~/cognee/cognee && uv run pytest -x", 0, "success"),
        ("cd ~/cognee/cognee && uv run pytest -x", 1, "error"),
        ("git commit -m cognee", 0, "success"),
        ("mkdir cognee", 0, "success"),
    ],
)
def test_bash_work_is_retained_across_hosts(
    suite, hook_module, monkeypatch, command, code, expected
):
    store = hook_module(suite, "store-to-session.py")
    if suite.name == "claude-code":
        monkeypatch.setattr(store, "cloud_enabled", lambda *a: False)
    monkeypatch.setattr(store, "_load_session", lambda: ("session", "dataset", "user"))
    monkeypatch.setattr(store, "load_config", lambda: {})
    monkeypatch.setattr(store, "resolve_runtime_mode", lambda: {"mode": "http", "base_url": ""})
    monkeypatch.setattr(store, "server_usable", lambda *a: False)
    writes = []
    monkeypatch.setattr(store, "append_warmup_entry", lambda *a: writes.append(a[2]))
    monkeypatch.setattr(store, "bump_save_counter", lambda *a, **k: None)
    asyncio.run(
        store._store_tool_call(
            {
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "tool_response": {"stdout": "", "exit_code": code},
            }
        )
    )
    assert len(writes) == 1
    assert writes[0]["status"] == expected


@pytest.mark.parametrize(
    "response,captured",
    [
        ({"stdout": "", "stderr": "", "interrupted": False, "isImage": False}, False),
        ({"stdout": "", "stderr": "", "interrupted": False, "noOutputExpected": True}, False),
        ({"stdout": "built 3 targets", "stderr": "", "interrupted": False}, True),
        ({"stdout": "", "stderr": "", "interrupted": True}, True),
    ],
)
def test_cloud_skips_silent_bash_success(suite, hook_module, monkeypatch, response, captured):
    # Real Claude Code Bash responses carry isImage/noOutputExpected flags; a
    # mutation that printed nothing adds no evidence to cloud memory.
    if suite.name != "claude-code":
        pytest.skip("Claude Code cloud capture")
    store = hook_module(suite, "store-to-session.py")
    monkeypatch.setattr(store, "cloud_enabled", lambda *a: True)

    class Captured(Exception):
        pass

    # Loading the session is the first step after the skip decision.
    monkeypatch.setattr(store, "_load_session", Mock(side_effect=Captured))
    call = {"tool_name": "Bash", "tool_input": {"command": "mkdir build"}}
    call["tool_response"] = response
    try:
        asyncio.run(store._store_tool_call(call))
        reached = False
    except Captured:
        reached = True
    assert reached is captured


@pytest.mark.parametrize(
    "command,event",
    [
        ("cognee-cli recall SENTINEL-7c1d", "skip_self_cognee_bash"),
        ("cd /tmp/SENTINEL-7c1d-cognee && grep -rn SENTINEL-7c1d src/", "trace_skipped_low_signal"),
        ("python3 /tmp/SENTINEL-7c1d/cognee-search.sh q", "skip_self_cognee_bash"),
    ],
)
def test_skip_logs_never_include_command_text(
    suite, hook_module, monkeypatch, temp_home, command, event
):
    store = hook_module(suite, "store-to-session.py")
    monkeypatch.setattr(store, "_load_session", Mock(side_effect=AssertionError("must skip")))
    asyncio.run(
        store._store_tool_call(
            {
                "tool_name": "Bash",
                "tool_input": {"command": command},
                "tool_response": {"stdout": "SENTINEL-7c1d", "exit_code": 0},
            }
        )
    )
    logs = list(temp_home.rglob("hook.log"))
    assert logs
    content = "\n".join(p.read_text() for p in logs)
    assert event in content
    assert "SENTINEL-7c1d" not in content
    assert "cmd_prefix" not in content


@pytest.mark.parametrize(
    "command,skip",
    [
        ("git branch --list -D cognee", False),
        ("git log --output=report", False),
        ("uniq input output", False),
        ("cd cognee & ls", False),
        ("sed -n '1,120p' cognee.py", True),
        ("sed 'w output' input", False),
        ("awk '{print}' cognee.py", True),
        ("awk 'system(\"rm foo\")'", False),
    ],
)
def test_inspection_options_do_not_hide_writes(policy, command, skip):
    assert policy.low_signal_bash(command, {"exit_code": 0}, "success") is skip


# --- <private> spans and sanitize() (SDK-1100) -------------------------------


def test_private_spans_nest_and_an_unclosed_one_runs_to_the_end(policy):
    text = "a <private>x <private>y</private> z</private> b\n<private>tail"
    result = policy.sanitize(text)
    for hidden in ("x", "y", "z", "tail"):
        assert hidden not in result
    assert result == "a [private] b\n[private]"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("keep <private attr=1>one\r\ntwo</private> end", "keep [private] end"),
        ("line1\r\n<private>\r\nhidden\r\n</private>\r\nline2", "line1\r\n[private]\r\nline2"),
        ("<PRIVATE>hidden</Private>shown", "[private]shown"),
        ("empty <private/> tag", "empty  tag"),
        ("stray </private> close", "stray  close"),
        ("<privateer>not a span</privateer>", "<privateer>not a span</privateer>"),
        ("no tags at all", "no tags at all"),
    ],
)
def test_private_span_forms(policy, text, expected):
    assert policy.sanitize(text) == expected


def test_sanitize_walks_containers_and_still_redacts_secrets(policy):
    value = {"prompt": "ok <private>hidden</private> password=hunter2", "api_key": "v", "n": 3}
    assert policy.sanitize(value) == {
        "prompt": "ok [private] [redacted:credential]",
        "api_key": "[redacted:credential]",
        "n": 3,
    }
    assert policy.sanitize(["<private>a</private>b"]) == ["[private]b"]


@pytest.mark.parametrize("variable", ["COGNEE_CAPTURE", "COGNEE_CAPTURE_REDACT"])
def test_spans_are_stripped_whatever_the_capture_switches_say(policy, monkeypatch, variable):
    monkeypatch.setenv(variable, "false")
    result = policy.sanitize("a <private>hidden</private> password=hunter2")
    assert "hidden" not in result
    if variable == "COGNEE_CAPTURE_REDACT":
        assert "password=hunter2" in result  # the switch still turns off the patterns
    else:
        assert "hunter2" not in result


def test_sanitize_cli_prints_the_clean_text_and_fails_closed(suite, temp_home):
    import os
    import subprocess
    import sys

    def cli(text, **env):
        return subprocess.run(
            [sys.executable, str(suite.scripts_dir / "_capture_policy.py"), text],
            env={**os.environ, "HOME": str(temp_home), **env},
            capture_output=True,
            text=True,
        )

    ok = cli("find <private>SENTINEL</private> token=abc")
    assert ok.returncode == 0 and ok.stdout == "find [private] [redacted:credential]"
    bad = cli("<private>SENTINEL</private>", COGNEE_CAPTURE_REDACT_PATTERNS='["["]')
    assert bad.returncode != 0 and bad.stdout == ""
    assert "SENTINEL" not in bad.stderr


def test_shared_policy_modules_are_identical_across_hosts():
    from utils.suites import ANTIGRAVITY, CLAUDE, CODEX

    for name in ("_capture_policy.py", "_remember_http.py"):
        reference = (CLAUDE.scripts_dir / name).read_bytes()
        for other in (CODEX, ANTIGRAVITY):
            assert (other.scripts_dir / name).read_bytes() == reference, f"{other.name}/{name}"


def test_sync_payload_log_names_fields_but_never_their_values(suite, run_hook, temp_home):
    result = run_hook(
        suite,
        "sync-session-to-graph.py",
        stdin={"session_id": "s1", "prompt": "SENTINEL-4b2e <private>x</private>"},
    )
    assert result.returncode == 0, result.stderr
    lines = [
        json.loads(line)
        for path in temp_home.rglob("hook.log")
        for line in path.read_text(encoding="utf-8").splitlines()
        if '"sync_payload"' in line
    ]
    assert lines
    log_text = json.dumps(lines)
    assert "payload_preview" not in log_text
    assert "SENTINEL-4b2e" not in log_text
    assert "prompt" in log_text and "payload_bytes" in log_text
