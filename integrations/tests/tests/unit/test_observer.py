"""Claude observer (Claude Code only): local mode without an LLM_API_KEY.

Two modules share the feature:

* ``_observer`` — the decision (``resolve_observer``) SessionStart makes before
  the install/boot, and the environment it applies so cognee's ``custom``
  provider talks to the shim (``apply_observer_env``);
* ``claude-observer.py`` — the OpenAI-compatible shim that turns one
  ``/v1/chat/completions`` request into one ``claude -p --safe-mode`` run.

No ``claude`` binary, no server and no subprocess here: the decision is driven
through env/config, and the shim's translation is tested around a faked
``_run_claude``. The idle watcher's observer branch is covered at the end.
"""

from __future__ import annotations

import json
import os
import stat

import pytest


@pytest.fixture
def observer(suite, isolated_modules, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: the Claude observer is a Claude Code feature")
    module = isolated_modules(suite, "_observer")
    # The isolation env turns the observer off for every other test.
    monkeypatch.delenv("COGNEE_LLM_OBSERVER", raising=False)
    # apply_observer_env writes these straight into os.environ. Registering
    # them with monkeypatch restores them at teardown; without it a leaked
    # EMBEDDING_PROVIDER=fastembed adds the fastembed extra to every later
    # install-spec test. Clearing them also keeps a developer's own
    # LLM_API_KEY from turning the "no key" tests into "key" tests.
    # setenv first: delenv on an unset key records nothing, so a value the
    # test sets afterwards would survive teardown.
    for key in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_ENDPOINT",
        "LLM_API_KEY",
        "EMBEDDING_PROVIDER",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
        module.ACTIVE_ENV_FLAG,
    ):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    return module


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A `claude` on PATH (so ``find_claude`` resolves) that is never executed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # shutil.which only matches PATHEXT extensions on Windows.
    if os.name == "nt":
        exe = bin_dir / "claude.cmd"
        exe.write_text("@exit /b 0\r\n", encoding="utf-8")
    else:
        exe = bin_dir / "claude"
        exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bin_dir))
    return str(exe)


# ── the decision ─────────────────────────────────────────────────────────────


def test_auto_activates_when_no_key_and_claude_present(observer, fake_claude):
    decision = observer.resolve_observer({})
    assert decision["active"] is True
    # Windows: which() spells the extension as PATHEXT does (claude.CMD).
    assert os.path.normcase(decision["claude"]) == os.path.normcase(fake_claude)
    assert decision["reason"] == ""
    assert decision["error"] == ""
    assert decision["endpoint"].endswith("/v1")
    assert decision["model"] == observer.DEFAULT_CLAUDE_MODEL


def test_auto_yields_to_a_configured_key(observer, fake_claude, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-real")
    decision = observer.resolve_observer({})
    assert decision["active"] is False
    assert decision["reason"] == "llm_key_configured"
    assert decision["error"] == ""


def test_auto_yields_to_a_configured_provider(observer, fake_claude, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    decision = observer.resolve_observer({})
    assert decision["active"] is False
    assert decision["reason"] == "llm_provider_configured"


def test_cloud_mode_never_runs_the_observer(observer, fake_claude):
    assert (
        observer.resolve_observer({"base_url": "https://api.cognee.ai"})["reason"] == "cloud_mode"
    )
    assert observer.resolve_observer({"_forced_backend": "cloud"})["reason"] == "cloud_mode"


@pytest.mark.parametrize(
    "url", ["http://localhost:8011", "http://127.0.0.1:8011", "http://[::1]:8011"]
)
def test_local_base_url_is_not_cloud(observer, fake_claude, url):
    """SessionStart fills base_url with the local server's URL before deciding."""
    assert observer.resolve_observer({"base_url": url})["active"] is True


def test_auto_yields_to_a_key_in_the_servers_dotenv(observer, fake_claude, temp_home):
    """cognee's load_dotenv(override=True) makes that key the server's, unseen here."""
    (temp_home / ".cognee-plugin" / "venv").mkdir(parents=True, exist_ok=True)
    dotenv = temp_home / ".cognee-plugin" / ".env"
    dotenv.write_text('LLM_API_KEY="sk-in-dotenv"\n', encoding="utf-8")
    decision = observer.resolve_observer({})
    assert decision["active"] is False
    assert decision["reason"] == "server_dotenv_configured"
    assert decision["dotenv"] == str(dotenv.resolve())
    assert "server's" in observer.describe(decision)


def test_server_dotenv_with_our_own_key_does_not_count(observer, fake_claude, temp_home):
    (temp_home / ".cognee-plugin" / "venv").mkdir(parents=True, exist_ok=True)
    token = observer.observer_token()
    (temp_home / ".cognee-plugin" / ".env").write_text(
        f"LLM_API_KEY={token}\nEMBEDDING_PROVIDER=fastembed\n", encoding="utf-8"
    )
    assert observer.resolve_observer({})["active"] is True


def test_token_is_created_once_and_private(observer):
    token = observer.observer_token()
    assert len(token) >= 32
    assert observer.observer_token() == token
    assert observer.observer_token(create=False) == token
    if os.name != "nt":
        assert stat.S_IMODE(observer.TOKEN_FILE.stat().st_mode) == 0o600


def test_token_loser_of_a_creation_race_gets_the_winners_token(observer, monkeypatch):
    """os.link is the exclusive step: whoever links second reads the winner's file."""
    real_link = os.link

    def racing_link(src, dst):
        observer.TOKEN_FILE.write_text("winner-token", encoding="utf-8")
        return real_link(src, dst)  # target now exists -> FileExistsError

    monkeypatch.setattr(observer.os, "link", racing_link)
    assert observer.observer_token() == "winner-token"
    assert not list(observer._STATE_DIR.glob(".token-*"))  # temp file cleaned up


def test_token_is_never_visible_empty(observer, monkeypatch):
    """The file appears with its content (hard link of a written temp file)."""
    seen: list[str] = []
    real_link = os.link

    def watching_link(src, dst):
        real_link(src, dst)
        seen.append(observer.TOKEN_FILE.read_text(encoding="utf-8"))

    monkeypatch.setattr(observer.os, "link", watching_link)
    token = observer.observer_token()
    assert seen == [token] and token


def test_empty_token_file_mid_write_is_waited_for(observer, monkeypatch):
    """An older writer that created the file but has not written it yet."""
    observer._STATE_DIR.mkdir(parents=True, exist_ok=True)
    observer.TOKEN_FILE.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        observer.time,
        "sleep",
        lambda _s: observer.TOKEN_FILE.write_text("late-token", encoding="utf-8"),
    )
    assert observer.observer_token() == "late-token"


def test_abandoned_empty_token_file_is_replaced(observer, monkeypatch):
    """A writer that died between create and write must not break the shim forever."""
    observer._STATE_DIR.mkdir(parents=True, exist_ok=True)
    observer.TOKEN_FILE.write_text("", encoding="utf-8")
    old = observer.TOKEN_FILE.stat().st_mtime - 60
    os.utime(observer.TOKEN_FILE, (old, old))
    monkeypatch.setattr(observer.time, "sleep", lambda _s: None)
    token = observer.observer_token()
    assert len(token) >= 32
    assert observer.observer_token(create=False) == token


def test_fresh_empty_token_file_is_not_stolen(observer, monkeypatch):
    """Still empty after the retries but young: its writer may be alive; do not replace."""
    observer._STATE_DIR.mkdir(parents=True, exist_ok=True)
    observer.TOKEN_FILE.write_text("", encoding="utf-8")
    monkeypatch.setattr(observer.time, "sleep", lambda _s: None)
    assert observer.observer_token() == ""
    assert observer.TOKEN_FILE.read_text(encoding="utf-8") == ""


def test_token_without_hard_links_falls_back_to_exclusive_create(observer, monkeypatch):
    def no_links(src, dst):
        raise PermissionError("hard links not supported")

    monkeypatch.setattr(observer.os, "link", no_links)
    token = observer.observer_token()
    assert len(token) >= 32
    assert observer.observer_token(create=False) == token
    if os.name != "nt":
        assert stat.S_IMODE(observer.TOKEN_FILE.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "value",
    [
        "haiku",
        "sonnet[1m]",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4@20250514",
        "us.anthropic.claude-sonnet-4-20250514-v1:0",
        "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc",
    ],
)
def test_real_model_ids_are_accepted(observer, monkeypatch, value):
    monkeypatch.setenv("COGNEE_OBSERVER_MODEL", value)
    assert observer.claude_model() == value
    assert observer.model_warning() == ""


@pytest.mark.parametrize(
    "value",
    [
        "opus --verbose --output-file /tmp/leak",
        "--dangerously-skip-permissions",
        "../../../etc/passwd",
        "'; drop table users--",
        "haiku\nsonnet",
        "x" * 201,
    ],
)
def test_unusable_model_falls_back_with_a_clear_warning(observer, fake_claude, monkeypatch, value):
    monkeypatch.setenv("COGNEE_OBSERVER_MODEL", value)
    assert observer.claude_model() == observer.DEFAULT_CLAUDE_MODEL
    warning = observer.model_warning()
    assert "COGNEE_OBSERVER_MODEL" in warning and "not a valid model name" in warning
    assert f"uses {observer.DEFAULT_CLAUDE_MODEL} instead" in warning
    decision = observer.resolve_observer({})
    # The observer still runs — on the default model — and says why.
    assert decision["active"] is True
    assert decision["model"] == observer.DEFAULT_CLAUDE_MODEL
    assert decision["model_warning"] == warning


def test_unset_model_has_no_warning(observer, fake_claude):
    assert "model_warning" not in observer.resolve_observer({})


def test_disabled_is_final(observer, fake_claude, monkeypatch):
    monkeypatch.setenv("COGNEE_LLM_OBSERVER", "false")
    decision = observer.resolve_observer({})
    assert decision["active"] is False
    assert decision["reason"] == "disabled"


def test_missing_cli_is_silent_in_auto_and_loud_when_forced(observer, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing on PATH
    monkeypatch.setenv("HOME", str(tmp_path))
    auto = observer.resolve_observer({})
    assert auto["active"] is False
    assert auto["reason"] == "claude_cli_missing"
    assert auto["error"] == ""

    monkeypatch.setenv("COGNEE_LLM_OBSERVER", "true")
    forced = observer.resolve_observer({})
    assert forced["active"] is False
    assert "COGNEE_LLM_OBSERVER=true" in forced["error"]
    assert "COGNEE_OBSERVER_CLAUDE" in forced["error"]


def test_forced_true_overrides_a_configured_key(observer, fake_claude, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-real")
    monkeypatch.setenv("COGNEE_LLM_OBSERVER", "true")
    assert observer.resolve_observer({})["active"] is True


def test_explicit_claude_path_wins(observer, tmp_path, monkeypatch):
    exe = tmp_path / "my-claude"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("COGNEE_OBSERVER_CLAUDE", str(exe))
    assert observer.find_claude() == str(exe)


# ── the environment cognee sees ──────────────────────────────────────────────


def test_apply_points_cognee_at_the_shim(observer, fake_claude, monkeypatch):
    monkeypatch.setenv("COGNEE_OBSERVER_PORT", "8123")
    decision = observer.resolve_observer({})
    applied = observer.apply_observer_env(decision)
    assert os.environ["LLM_PROVIDER"] == "custom"
    assert os.environ["LLM_MODEL"] == observer.COGNEE_MODEL == "openai/" + observer.MODEL_ALIAS
    assert os.environ["LLM_ENDPOINT"] == "http://127.0.0.1:8123/v1"
    # cognee's LLM_API_KEY is the shim's bearer token, not a guessable constant.
    assert os.environ["LLM_API_KEY"] == observer.observer_token(create=False) != ""
    assert os.environ[observer.ACTIVE_ENV_FLAG] == "1"
    # Embeddings default to a local model — the observer cannot embed.
    assert os.environ["EMBEDDING_PROVIDER"] == "fastembed"
    assert os.environ["EMBEDDING_MODEL"] == observer.DEFAULT_EMBEDDING_MODEL
    assert os.environ["EMBEDDING_DIMENSIONS"] == observer.DEFAULT_EMBEDDING_DIMENSIONS
    assert set(applied) >= {
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_ENDPOINT",
        "LLM_API_KEY",
        "EMBEDDING_PROVIDER",
    }
    assert observer.is_active()


def test_apply_keeps_a_user_embedder(observer, fake_claude, monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    monkeypatch.setenv("EMBEDDING_MODEL", "nomic-embed-text")
    applied = observer.apply_observer_env(observer.resolve_observer({}))
    assert os.environ["EMBEDDING_PROVIDER"] == "ollama"
    assert os.environ["EMBEDDING_MODEL"] == "nomic-embed-text"
    assert "EMBEDDING_PROVIDER" not in applied
    # Dimensions were unset, so the default lands — a model without dims is unusable.
    assert os.environ["EMBEDDING_DIMENSIONS"] == observer.DEFAULT_EMBEDDING_DIMENSIONS


def test_apply_is_a_no_op_for_an_inactive_decision(observer, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert observer.apply_observer_env({"active": False}) == {}
    assert "LLM_PROVIDER" not in os.environ
    assert not observer.is_active()


def test_re_resolve_after_apply_stays_active(observer, fake_claude):
    """The placeholder key/custom provider the apply wrote are ours, not a user's."""
    observer.apply_observer_env(observer.resolve_observer({}))
    assert observer.resolve_observer({})["active"] is True


def test_is_active_falls_back_to_the_launch_record(observer, suite, isolated_modules, monkeypatch):
    """A watcher re-spawned from a hook has none of SessionStart's env."""
    pc = isolated_modules(suite, "_plugin_common")
    monkeypatch.delenv(observer.ACTIVE_ENV_FLAG, raising=False)
    monkeypatch.setattr(pc, "get_session_key", lambda: "host-1")
    monkeypatch.setattr(pc, "_read_map_record", lambda key: {"llm_observer": {"active": True}})
    assert observer.is_active()
    monkeypatch.setattr(pc, "_read_map_record", lambda key: {"llm_observer": {"active": False}})
    assert not observer.is_active()
    monkeypatch.setattr(pc, "_read_map_record", lambda key: {})
    assert not observer.is_active()


# ── the shim's request translation ───────────────────────────────────────────


@pytest.fixture
def shim(suite, hook_module, observer):
    return hook_module(suite, "claude-observer.py")


def test_messages_split_into_system_and_prompt(shim):
    system, prompt = shim.split_messages(
        [
            {"role": "system", "content": "Be terse."},
            {"role": "developer", "content": [{"type": "text", "text": "Return JSON."}]},
            {"role": "user", "content": "Extract entities from: hello"},
        ]
    )
    assert system == "Be terse.\n\nReturn JSON."
    assert prompt == "Extract entities from: hello"


def test_multi_turn_is_flattened_with_roles(shim):
    _, prompt = shim.split_messages(
        [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
    )
    assert prompt == "User: q1\n\nAssistant: a1\n\nUser: q2"


def test_schema_extraction(shim):
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    body = {
        "response_format": {"type": "json_schema", "json_schema": {"name": "X", "schema": schema}}
    }
    assert shim.extract_schema(body) == (schema, True)
    assert shim.extract_schema({"response_format": {"type": "json_object"}}) == (None, True)
    assert shim.extract_schema({}) == (None, False)


def test_model_alias_resolves_to_configured_claude_model(shim, observer, monkeypatch):
    assert shim.resolve_model("openai/" + observer.MODEL_ALIAS) == observer.DEFAULT_CLAUDE_MODEL
    monkeypatch.setenv("COGNEE_OBSERVER_MODEL", "sonnet")
    assert shim.resolve_model(observer.MODEL_ALIAS) == "sonnet"
    assert shim.resolve_model("openai/opus") == "opus"
    assert shim.resolve_model("") == "sonnet"
    # A request naming something that cannot be a model gets the configured one.
    assert shim.resolve_model("--dangerously-skip-permissions") == "sonnet"
    assert shim.resolve_model("opus --verbose") == "sonnet"


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("Not logged in. Please run /login", 401),
        ("Invalid API key · Fix external API key", 401),
        ("OAuth token has expired", 401),
        ("Rate limit reached for this hour", 429),
        ("Error: overloaded (529)", 429),
        ("some unexpected crash", 502),
    ],
)
def test_failure_classification(shim, text, status):
    assert shim.classify_failure(text) == status


def test_child_env_drops_the_parent_claude_session(shim, observer, monkeypatch):
    for key in (
        "CLAUDECODE",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_PID",
    ):
        monkeypatch.setenv(key, "parent")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/home/u/.claude")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = shim.child_env()
    for key in (
        "CLAUDECODE",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_PID",
    ):
        assert key not in env
    assert env["CLAUDE_CONFIG_DIR"] == "/home/u/.claude"  # credentials live here
    assert env[observer.CHILD_ENV_FLAG] == "1"
    assert env["PATH"] == "/usr/bin"


def test_child_env_keeps_auth_and_drops_the_api_key(shim, monkeypatch):
    """Token logins and Bedrock/Vertex must work; an API key must not move the bill."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-x")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-x")
    env = shim.child_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-x"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert "ANTHROPIC_API_KEY" not in env


def test_complete_returns_structured_output_as_content(shim, monkeypatch):
    seen = {}

    def fake_run(claude, *, model, system_prompt, prompt, schema, timeout):
        seen.update(model=model, system_prompt=system_prompt, prompt=prompt, schema=schema)
        return {
            "result": "ignored when structured_output is present",
            "structured_output": {"a": "b"},
            "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 5},
            "modelUsage": {"claude-haiku-4-5": {}},
            "total_cost_usd": 0.0001,
        }

    monkeypatch.setattr(shim, "_run_claude", fake_run)
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    response = shim.complete(
        {
            "model": "openai/claude-observer",
            "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "T", "schema": schema},
            },
        },
        claude="/bin/claude",
    )
    assert seen["schema"] == schema and seen["prompt"] == "go" and seen["system_prompt"] == "sys"
    choice = response["choices"][0]
    assert json.loads(choice["message"]["content"]) == {"a": "b"}
    assert choice["finish_reason"] == "stop"
    assert response["model"] == "openai/claude-observer"
    assert response["usage"]["prompt_tokens"] == 15  # input + cache read
    assert response["usage"]["completion_tokens"] == 2
    assert response["cognee_observer"]["claude_model"] == "claude-haiku-4-5"


def test_complete_strips_fences_for_plain_json_requests(shim, monkeypatch):
    monkeypatch.setattr(
        shim,
        "_run_claude",
        lambda *a, **k: {"result": '```json\n{"x": 1}\n```', "usage": {}},
    )
    response = shim.complete(
        {
            "messages": [{"role": "user", "content": "go"}],
            "response_format": {"type": "json_object"},
        },
        claude="/bin/claude",
    )
    assert response["choices"][0]["message"]["content"] == '{"x": 1}'


def test_complete_retries_without_schema_when_the_cli_rejects_it(shim, monkeypatch):
    attempts = []

    def fake_run(claude, *, model, system_prompt, prompt, schema, timeout):
        attempts.append(schema)
        if schema is not None:
            raise shim.ObserverError(
                502, "claude: unsupported JSON schema keyword", "claude_failed"
            )
        assert "matching this schema" in prompt
        return {"result": '{"a": "ok"}', "usage": {}}

    monkeypatch.setattr(shim, "_run_claude", fake_run)
    response = shim.complete(
        {
            "messages": [{"role": "user", "content": "go"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"schema": {"type": "object"}},
            },
        },
        claude="/bin/claude",
    )
    assert attempts == [{"type": "object"}, None]
    assert response["choices"][0]["message"]["content"] == '{"a": "ok"}'


def test_complete_rejects_empty_requests(shim):
    with pytest.raises(shim.ObserverError) as exc:
        shim.complete({"messages": []}, claude="/bin/claude")
    assert exc.value.status == 400


def test_forced_tool_becomes_a_tool_call(shim, monkeypatch):
    monkeypatch.setattr(
        shim, "_run_claude", lambda *a, **k: {"structured_output": {"q": "x"}, "usage": {}}
    )
    tools = [
        {"type": "function", "function": {"name": "extract", "parameters": {"type": "object"}}}
    ]
    response = shim.complete(
        {
            "messages": [{"role": "user", "content": "go"}],
            "tools": tools,
            "tool_choice": {"type": "function", "function": {"name": "extract"}},
        },
        claude="/bin/claude",
    )
    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]["function"]
    assert call["name"] == "extract"
    assert json.loads(call["arguments"]) == {"q": "x"}


# ── the shim's access control ────────────────────────────────────────────────


@pytest.fixture
def live_shim(shim, observer):
    """The real HTTP handler on an ephemeral loopback port (no `claude` is run)."""
    import threading
    from http.server import ThreadingHTTPServer

    token = observer.observer_token()
    shim.Handler.state = shim.State("/bin/claude", "", token)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), shim.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", token
    httpd.shutdown()
    httpd.server_close()


def _request(url, *, headers=None, data=None):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_shim_requires_the_token(live_shim):
    base, token = live_shim
    assert _request(base + "/v1/models") == 401
    assert _request(base + "/v1/models", headers={"Authorization": "Bearer wrong"}) == 401
    assert _request(base + "/v1/models", headers={"Authorization": f"Bearer {token}"}) == 200
    assert _request(base + "/v1/chat/completions", data=b"{}") == 401
    assert _request(base + "/v1/observer/shutdown", data=b"{}") == 401
    assert _request(base + "/health") == 200  # liveness stays open


def _raw_post(base, path, body, headers=""):
    """POST over a bare socket; return the status line's code, or the socket error.

    urllib hides which side dropped the connection; this shows whether the
    client got the response or a reset (Windows reports an unread request body
    as WinError 10053/10054 when the server closes without reading it).
    """
    import socket

    host, port = base.rsplit("//", 1)[1].split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(
            (
                f"POST {path} HTTP/1.1\r\nHost: {host}\r\n{headers}"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            + body
        )
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    return int(data.split(b" ", 2)[1])


@pytest.mark.parametrize(
    ("path", "headers", "status"),
    [
        ("/v1/chat/completions", "", 401),
        ("/v1/chat/completions", "Origin: https://evil.example\r\n", 403),
        ("/v1/nope", "AUTH", 404),
        ("/v1/embeddings", "AUTH", 501),
    ],
)
def test_shim_reads_the_body_before_answering(live_shim, path, headers, status):
    """Every early answer drains the request body, so the client gets the status."""
    base, token = live_shim
    headers = headers.replace("AUTH", f"Authorization: Bearer {token}\r\n")
    body = b'{"messages": [{"role": "user", "content": "' + b"x" * 200_000 + b'"}]}'
    assert _raw_post(base, path, body, headers) == status


def test_shim_refuses_browser_requests(live_shim):
    """A page can POST to loopback without CORS; a token-less one gets nothing, and
    even a request carrying the token is refused once it comes with an Origin."""
    base, token = live_shim
    origin = {"Origin": "https://evil.example", "Content-Type": "text/plain"}
    assert _request(base + "/v1/chat/completions", headers=origin, data=b"{}") == 403
    assert _request(base + "/health", headers=origin) == 403
    assert (
        _request(base + "/v1/models", headers={**origin, "Authorization": f"Bearer {token}"}) == 403
    )


def test_client_helpers_send_the_token(live_shim, observer, monkeypatch):
    base, _ = live_shim
    port = int(base.rsplit(":", 1)[1])
    assert observer.observer_alive(port)
    status, _ = observer._http_get(base + "/v1/models", timeout=5)
    assert status == 200


# ── session start: the note and a server that is already running ─────────────


@pytest.fixture
def session_start(suite, hook_module, observer, monkeypatch):
    module = hook_module(suite, "session-start.py")
    monkeypatch.delenv("COGNEE_LLM_OBSERVER", raising=False)  # re-isolated by the load
    return module


def _decide(session_start, monkeypatch, *, running, stamp):
    monkeypatch.setattr(session_start, "_running_server_observer", lambda url: (running, stamp))
    import _observer as obs

    monkeypatch.setattr(obs, "ensure_observer_running", lambda *a, **k: True)
    return session_start._apply_observer(
        {"base_url": "http://localhost:8011"}, "http://localhost:8011"
    )


def test_note_warns_about_spend_and_embeddings(session_start, fake_claude, monkeypatch):
    decision = _decide(session_start, monkeypatch, running=False, stamp=None)
    assert decision["active"] is True
    note = session_start._observer_note(decision)
    assert "Claude subscription" in note and "usage limits" in note
    assert "switch to a new dataset" in note
    # Claude Code displays only the top-level systemMessage.
    output = session_start._with_observer_note({"hookSpecificOutput": {}}, decision)
    assert output["systemMessage"] == note


def test_note_carries_the_model_warning(session_start, fake_claude, monkeypatch):
    monkeypatch.setenv("COGNEE_OBSERVER_MODEL", "opus --verbose")
    decision = _decide(session_start, monkeypatch, running=False, stamp=None)
    assert decision["active"] is True and decision["model"] == "haiku"
    note = session_start._observer_note(decision)
    assert "model haiku" in note
    assert "Warning: COGNEE_OBSERVER_MODEL='opus --verbose' is not a valid model name" in note


def test_running_keyed_server_is_not_claimed(session_start, fake_claude, monkeypatch):
    decision = _decide(session_start, monkeypatch, running=True, stamp=False)
    assert decision["active"] is False
    assert os.environ.get("LLM_PROVIDER") != "custom"  # env left alone
    assert "not applied" in session_start._observer_note(decision)


def test_running_observer_server_is_followed_even_with_a_key(
    session_start, fake_claude, monkeypatch
):
    monkeypatch.setenv("LLM_API_KEY", "sk-real")
    decision = _decide(session_start, monkeypatch, running=True, stamp=True)
    assert decision["active"] is True and decision["wanted"] is False
    note = session_start._observer_note(decision)
    assert "still uses your subscription" in note


def test_running_server_of_unknown_origin_is_not_claimed(session_start, fake_claude, monkeypatch):
    decision = _decide(session_start, monkeypatch, running=True, stamp=None)
    note = session_start._observer_note(decision)
    assert "unknown which LLM" in note
    assert not note.startswith("⚠ LLM: Claude Code")


# ── the idle watcher asks the shim, not litellm ──────────────────────────────


@pytest.fixture
def watcher_check(suite, hook_module, isolated_modules, observer, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: the Claude observer is a Claude Code feature")
    import sys

    watcher = hook_module(suite, "idle-watcher.py")
    # The watcher imports both lazily, at call time, from sys.modules. Each
    # isolated_modules() call pops every isolated module, so the first one
    # loaded must be put back for the patches on it to be the ones seen.
    obs = isolated_modules(suite, "_observer")
    pc = isolated_modules(suite, "_plugin_common")
    monkeypatch.setitem(sys.modules, "_observer", obs)
    writes, events = [], []
    monkeypatch.setattr(
        pc, "write_llm_state", lambda state, detail="", reason="": writes.append((state, reason))
    )
    monkeypatch.setattr(pc, "read_llm_state", lambda: {})
    monkeypatch.setattr(pc, "get_session_key", lambda: "host-1")
    monkeypatch.setattr(watcher, "_log", lambda event, **kw: events.append(event))
    monkeypatch.setenv(obs.ACTIVE_ENV_FLAG, "1")

    def _run(verdict):
        monkeypatch.setattr(obs, "observer_probe", lambda **kw: verdict)
        watcher._check_llm_key({})
        return writes, events

    return _run


def test_watcher_marks_ok_when_claude_answers(watcher_check):
    writes, events = watcher_check({"auth": "ok", "detail": "", "status": 200})
    assert writes == [("ok", "")]
    assert "llm_key_ok" in events


def test_watcher_names_the_login_not_a_key(watcher_check):
    writes, events = watcher_check({"auth": "failed", "detail": "Not logged in", "status": 401})
    assert writes == [("auth_failed", "claude_not_logged_in")]
    assert "llm_key_auth_failed" in events


def test_watcher_leaves_marker_alone_when_shim_is_down(watcher_check):
    writes, events = watcher_check({"auth": "unknown", "detail": "connection refused", "status": 0})
    assert writes == []
    assert "llm_key_check_inconclusive" in events


def test_statusline_shows_the_specific_reason(statusline, suite, temp_home, monkeypatch):
    """`✕ (claude_not_logged_in)` rather than the misleading `incorrect_llm_api_key`."""
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: the Claude observer is a Claude Code feature")
    import time as _time

    marker_dir = temp_home / ".cognee-plugin" / suite.state_subdir / "llm-state"
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "host-1.json").write_text(
        json.dumps(
            {
                "llm_state": "auth_failed",
                "checked_at": _time.time(),
                "session_key": "host-1",
                "reason": "claude_not_logged_in",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(statusline, "_active_mode", lambda: "local")
    prefix = statusline._llm_prefix("host-1")
    assert "claude_not_logged_in" in prefix
    assert "incorrect_llm_api_key" not in prefix
