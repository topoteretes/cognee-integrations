"""SessionStart with settings from the plugin options only (SDK-1411, Claude Code).

- the local server starts with an allowlisted environment: no shell secret
  reaches it, the LLM / embedding options do, COGNEE_ENV_FILE points at an
  empty plugin-owned file and HASH_API_KEY is false;
- the option notices: the one-time ~/.cognee/.env upgrade notice, a Cloud
  URL without its key, a key without a URL;
- the launch record holds booleans and names, never a key.
"""

from __future__ import annotations

import importlib
import json
import os
import sys

import pytest


@pytest.fixture
def start(suite, hook_module):
    if not suite.keys_from_plugin_options:
        pytest.skip(f"{suite.name}: reads its settings from the environment and ~/.cognee/.env")
    return hook_module(suite, "session-start.py")


def _load_options(monkeypatch, *, hook: bool = True, **options) -> None:
    """Export plugin options and load them as a hook process does at start."""
    ef = sys.modules["_env_file"]
    if hook:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugin")
    for name, value in options.items():
        monkeypatch.setenv(f"CLAUDE_PLUGIN_OPTION_{name.upper()}", value)
    for name in list(os.environ):
        if name.startswith(("LLM_", "EMBEDDING_")):
            monkeypatch.setenv(name, os.environ[name])  # restored at teardown
    monkeypatch.delenv(ef._LLM_ENV_APPLIED, raising=False)
    monkeypatch.setattr(ef, "_loaded", False)
    ef.load_env_file()


# ── the server's environment (findings 1, 7, 9) ────────────────────────────


def test_shell_secrets_never_reach_the_server_and_the_options_do(start, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shell-openai")
    monkeypatch.setenv("GRAPH_DATABASE_PASSWORD", "pw-shell")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-shell")
    monkeypatch.setenv("LLM_API_KEY", "sk-shell-llm")
    monkeypatch.setenv("DB_PROVIDER", "postgres")
    monkeypatch.setenv("HASH_API_KEY", "true")
    monkeypatch.setenv("COGNEE_ENV_FILE", "/somewhere/else/.env")
    _load_options(
        monkeypatch,
        llm_api_key="sk-option-llm",
        llm_provider="openai",
        llm_model="openai/gpt-4o-mini",
        embedding_api_key="sk-option-embed",
        embedding_dimensions="3072",
    )
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "mine")  # an allowlisted tuning name

    env = start._server_env()

    for name in (
        "OPENAI_API_KEY",
        "GRAPH_DATABASE_PASSWORD",
        "AWS_SECRET_ACCESS_KEY",
        "DB_PROVIDER",
    ):
        assert name not in env, name
    assert "sk-shell" not in json.dumps(env)
    assert env["LLM_API_KEY"] == "sk-option-llm"
    assert env["LLM_PROVIDER"] == "openai"
    assert env["LLM_MODEL"] == "openai/gpt-4o-mini"
    assert env["EMBEDDING_API_KEY"] == "sk-option-embed"
    assert env["EMBEDDING_DIMENSIONS"] == "3072"
    assert env["HASH_API_KEY"] == "false"
    assert env["COGNEE_ENV_FILE"] == str(start._SERVER_ENV_FILE)
    assert start._SERVER_ENV_FILE.is_file()
    assert start._SERVER_ENV_FILE.read_text(encoding="utf-8") == ""
    assert env["COGNEE_AGENT_MODE"] == "true"
    assert env["DEFAULT_USER_EMAIL"] == "default_user@example.com"
    assert env["PATH"] == os.environ["PATH"]
    assert env["COGNEE_PLUGIN_DATASET"] == "mine"
    assert env["SYSTEM_ROOT_DIRECTORY"] == os.environ["SYSTEM_ROOT_DIRECTORY"]


def test_the_server_env_file_is_emptied_before_each_boot(start):
    start._SERVER_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    start._SERVER_ENV_FILE.write_text("LLM_API_KEY=planted\n", encoding="utf-8")
    start._server_env()
    assert start._SERVER_ENV_FILE.read_text(encoding="utf-8") == ""


def test_the_spawned_server_gets_the_allowlisted_env(start, monkeypatch):
    """The Popen the boot path makes, not just the helper."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shell-openai")
    _load_options(monkeypatch, llm_api_key="sk-option-llm")
    captured = {}

    class _Spawned(Exception):
        pass

    def fake_popen(args, **kwargs):
        captured.update(kwargs.get("env") or {})
        raise _Spawned()

    monkeypatch.setattr(start, "_health_ok", lambda *a, **k: False)
    monkeypatch.setattr(start, "server_presence", lambda *a, **k: (start.PRESENCE_ABSENT, {}))
    monkeypatch.setattr(start, "managed_endpoint_enabled", lambda *a, **k: False)
    monkeypatch.setattr(start, "ensure_cognee_installed", lambda *a, **k: True)
    monkeypatch.setattr(start, "_start_console_capture", lambda *a, **k: None)
    monkeypatch.setattr(start.subprocess, "Popen", fake_popen)

    with pytest.raises(_Spawned):
        start._ensure_local_server_running({"base_url": "http://localhost:59999"})
    assert captured, "the server was never spawned"
    assert "OPENAI_API_KEY" not in captured
    assert captured["LLM_API_KEY"] == "sk-option-llm"
    assert captured["HASH_API_KEY"] == "false"
    assert captured["COGNEE_ENV_FILE"] == str(start._SERVER_ENV_FILE)


# ── option notices (findings 2 and 4) ──────────────────────────────────────


def _write_env_file(temp_home, text: str) -> None:
    path = temp_home / ".cognee" / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_the_upgrade_notice_is_shown_once(start, temp_home, monkeypatch):
    _write_env_file(
        temp_home,
        'COGNEE_BASE_URL="https://tenant.cognee.ai"\nCOGNEE_API_KEY=ck_file\n'
        "COGNEE_RECALL_TIMEOUT=5\n",
    )
    _load_options(monkeypatch)
    first = start._option_notices()
    assert start.ENV_UPGRADE_NOTICE in first
    assert start.ENV_UPGRADE_NOTICE not in start._option_notices()
    flag = start._ENV_UPGRADE_NOTICE_FLAG.read_text(encoding="utf-8")
    assert "ck_file" not in flag and "tenant.cognee.ai" not in flag
    assert str(start._ENV_UPGRADE_NOTICE_FLAG).startswith(str(temp_home / ".cognee-plugin"))


def test_options_set_means_cloud_and_no_notice(start, temp_home, monkeypatch):
    _write_env_file(temp_home, 'COGNEE_BASE_URL="https://tenant.cognee.ai"\nCOGNEE_API_KEY=x\n')
    _load_options(
        monkeypatch, cognee_base_url="https://tenant.cognee.ai", cognee_api_key="ck_option"
    )
    assert start._option_notices() == []
    assert os.environ["COGNEE_BASE_URL"] == "https://tenant.cognee.ai"
    config = importlib.import_module("config")
    assert config.is_cloud_mode(config.load_config())


def test_a_url_without_its_key_is_named_every_session(start, monkeypatch):
    _load_options(monkeypatch, cognee_base_url="https://tenant.cognee.ai")
    assert start._option_notices() == [start.CLOUD_KEY_EMPTY_NOTICE]
    assert start._option_notices() == [start.CLOUD_KEY_EMPTY_NOTICE]
    assert "Cloud API key option is empty" in start.CLOUD_KEY_EMPTY_NOTICE


def test_a_key_without_a_url_runs_locally_and_says_so(start, monkeypatch):
    _load_options(monkeypatch, cognee_api_key="ck_option")
    notes = start._option_notices()
    assert notes == [start.CLOUD_URL_EMPTY_NOTICE]
    assert "running locally; set the URL to use Cloud" in notes[0]


def test_login_failure_messages_name_the_plugin_options(start):
    for body in ("does not have a password", "LOGIN_BAD_CREDENTIALS", "boom"):
        message = start._login_failure_message(400, body)
        assert "plugin's options" in message
        assert "COGNEE_USER_" not in message
        assert "COGNEE_API_KEY" not in message


# ── the launch record (finding 11) ─────────────────────────────────────────


def test_the_launch_record_holds_names_and_booleans_only(start, monkeypatch):
    pc = sys.modules["_plugin_common"]
    _load_options(
        monkeypatch,
        cognee_base_url="https://tenant.cognee.ai",
        cognee_api_key="ck_option_secret",
        llm_api_key="sk_option_secret",
    )
    pc._write_map_record("sess-facts", {"session_id": "s", "conn_uuid": "c", "cwd": "/p"})
    monkeypatch.setattr(pc, "get_session_key", lambda: "sess-facts")
    start._record_launch_facts({}, "https://tenant.cognee.ai")
    text = (pc._SESSIONS_MAP_DIR / "sess-facts.json").read_text(encoding="utf-8")
    assert "ck_option_secret" not in text and "sk_option_secret" not in text
    facts = json.loads(text)["launch_facts"]
    assert facts["cloud_key_option_set"] is True
    assert facts["llm_key_option_set"] is True
    assert facts["embedding_key_option_set"] is False
    assert facts["key_source"] == "plugin_option"
    assert "observer" not in facts
    assert pc.latest_launch_facts("/p")["key_source"] == "plugin_option"
