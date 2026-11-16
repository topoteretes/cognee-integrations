"""The Claude observer is gone (SDK-1411, 1.8.4).

Up to 1.8.3, local mode with no LLM key ran the server's LLM calls as
``claude -p`` under the user's stored Claude login, behind a loopback shim
with its own bearer token. The plugin directory holds versions that use a
credential from the user's machine, so the observer was removed. What must
hold afterwards:

- no shipped file starts ``claude``, names the shim or reads its token;
- a local session with no LLM key option is told so, and one with a key is not;
- an install whose last local launch ran on the observer keeps its embedder
  (fastembed), so the datasets it built stay searchable, unless the user set
  an embedding provider of their own;
- a server an older version started on the observer is left alone, and the
  shim's token and pidfile are deleted once that shim is gone.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

import pytest


@pytest.fixture
def start(suite, hook_module, monkeypatch, tmp_path):
    if not suite.keys_from_plugin_options:
        pytest.skip(f"{suite.name}: never had the Claude observer")
    module = hook_module(suite, "session-start.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(module, "_EMBEDDER_PIN", tmp_path / "state" / "embedder.json")
    monkeypatch.setattr(module, "_OBSERVER_STATE_DIR", tmp_path / "observer")
    for name in ("EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS", "LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_LLM_API_KEY", raising=False)
    return module


def _old_launch(pc, key: str, *, observer_active: bool, key_source: str, age: float = 0) -> None:
    """A launch record as 1.8.3 wrote it, ``observer`` fact included."""
    pc._write_map_record(key, {"session_id": key, "conn_uuid": "c", "cwd": "/p"})
    pc.record_launch_facts(key, {"key_source": key_source})
    record = pc._read_map_record(key)
    record["launch_facts"]["observer"] = {"active": observer_active, "reason": "", "model": ""}
    record["launch_facts"]["recorded_at"] = time.time() - age
    pc._write_map_record(key, record)


# ── nothing shipped runs or names the observer ─────────────────────────────


def test_no_shipped_file_runs_claude_or_names_the_shim(suite):
    if not suite.keys_from_plugin_options:
        pytest.skip(f"{suite.name}: never had the Claude observer")
    scripts = suite.scripts_dir
    assert not (scripts / "_observer.py").exists()
    assert not (scripts / "claude-observer.py").exists()
    pattern = re.compile(
        r"claude-observer|observer_token|\.credentials\.json|keychain|"
        r"[\"']claude[\"'],\s*[\"']-p[\"']|COGNEE_OBSERVER_(?!CHILD)|COGNEE_LLM_OBSERVER"
    )
    offenders = []
    for path in sorted(scripts.parent.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".sh", ".json", ""}:
            continue
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        offenders += [
            f"{path.relative_to(scripts.parent)}:{number}"
            for number, line in enumerate(text.splitlines(), 1)
            if pattern.search(line)
        ]
    assert offenders == []


def test_the_observer_settings_are_no_longer_tuning_names(suite, isolated_modules):
    if not suite.keys_from_plugin_options:
        pytest.skip(f"{suite.name}: never had the Claude observer")
    env_file = isolated_modules(suite, "_env_file")
    assert not [name for name in env_file._TUNING_NAMES if "OBSERVER" in name]


# ── the session-start note ─────────────────────────────────────────────────


def test_a_local_session_without_an_llm_key_is_told_so(start):
    llm = start._local_llm_state("http://localhost:8011")
    assert llm == {"configured": False, "embedder_pinned": False, "server_observer": False}
    note = start._llm_note(llm)
    assert "local mode has no LLM" in note
    assert "LLM API key option" in note
    assert "fastembed" not in note
    output = start._with_llm_note({"systemMessage": "Cognee Memory ready"}, llm)
    assert output["systemMessage"].startswith("Cognee Memory ready\n\n")
    assert "local mode has no LLM" in output["systemMessage"]


@pytest.mark.parametrize("option", ["llm_api_key", "llm_provider"])
def test_a_configured_llm_gets_no_note(start, monkeypatch, option):
    if option == "llm_api_key":
        monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_LLM_API_KEY", "sk-option")
    else:
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")  # as the option is applied
    llm = start._local_llm_state("http://localhost:8011")
    assert llm["configured"] is True
    assert start._llm_note(llm) == ""
    assert start._with_llm_note({"systemMessage": "x"}, llm) == {"systemMessage": "x"}


def test_cloud_mode_gets_no_note(start):
    assert start._llm_note({"cloud": True}) == ""


# ── the embedder of an install that ran on the observer ────────────────────


def test_an_observer_install_keeps_fastembed_also_after_a_key_is_added(start, monkeypatch):
    pc = sys.modules["_plugin_common"]
    _old_launch(pc, "old-local", observer_active=True, key_source="local_owner_key")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_LLM_API_KEY", "sk-option")

    llm = start._local_llm_state("http://localhost:8011")

    assert llm["embedder_pinned"] is True
    assert os.environ["EMBEDDING_PROVIDER"] == "fastembed"
    assert os.environ["EMBEDDING_MODEL"] == "BAAI/bge-small-en-v1.5"
    assert os.environ["EMBEDDING_DIMENSIONS"] == "384"
    pin = json.loads(start._EMBEDDER_PIN.read_text(encoding="utf-8"))
    assert pin["EMBEDDING_PROVIDER"] == "fastembed"
    # The server this launch boots is given that embedder, and needs its extra.
    assert start._server_env()["EMBEDDING_PROVIDER"] == "fastembed"


def test_the_pin_outlives_the_record_it_was_decided_from(start, monkeypatch):
    """Later launches record no observer fact; the pin file is what holds."""
    pc = sys.modules["_plugin_common"]
    _old_launch(pc, "old-local", observer_active=True, key_source="local_owner_key")
    assert start._apply_embedder_pin() is True
    for name in ("EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS"):
        monkeypatch.delenv(name, raising=False)
    (pc._SESSIONS_MAP_DIR / "old-local.json").unlink()
    assert start._apply_embedder_pin() is True
    assert os.environ["EMBEDDING_PROVIDER"] == "fastembed"


def test_the_pinned_note_names_the_kept_embedder(start):
    pc = sys.modules["_plugin_common"]
    _old_launch(pc, "old-local", observer_active=True, key_source="local_owner_key")
    note = start._llm_note(start._local_llm_state("http://localhost:8011"))
    assert "local mode has no LLM" in note
    assert "Embeddings stay on fastembed (BAAI/bge-small-en-v1.5, 384 dims)" in note


def test_the_users_own_embedding_provider_is_never_replaced(start, monkeypatch):
    pc = sys.modules["_plugin_common"]
    _old_launch(pc, "old-local", observer_active=True, key_source="local_owner_key")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")  # as the option is applied
    assert start._apply_embedder_pin() is False
    assert os.environ["EMBEDDING_PROVIDER"] == "openai"
    assert "EMBEDDING_MODEL" not in os.environ
    assert not start._EMBEDDER_PIN.exists()


@pytest.mark.parametrize(
    "records",
    [
        [],
        [("local", False, "local_owner_key", 0)],
        # The newest LOCAL launch decides: an older observer one was replaced
        # by a launch on the user's own key (and so their own embedder).
        [("older", True, "local_owner_key", 3600), ("newer", False, "local_owner_key", 0)],
    ],
    ids=["no record", "never on the observer", "moved off the observer since"],
)
def test_an_install_that_was_not_on_the_observer_is_not_pinned(start, records):
    pc = sys.modules["_plugin_common"]
    for key, active, source, age in records:
        _old_launch(pc, key, observer_active=active, key_source=source, age=age)
    assert start._apply_embedder_pin() is False
    assert "EMBEDDING_PROVIDER" not in os.environ
    assert not start._EMBEDDER_PIN.exists()


def test_a_cloud_launch_in_between_does_not_hide_the_observer_install(start):
    """A Cloud session says nothing about the local server's datasets."""
    pc = sys.modules["_plugin_common"]
    _old_launch(pc, "local", observer_active=True, key_source="local_owner_key", age=3600)
    _old_launch(pc, "cloud", observer_active=False, key_source="plugin_option", age=0)
    assert pc.last_local_launch_used_observer() is True
    assert start._apply_embedder_pin() is True


def test_this_version_records_no_observer_fact(start):
    pc = sys.modules["_plugin_common"]
    pc._write_map_record("now", {"session_id": "s", "conn_uuid": "c", "cwd": "/p"})
    pc.record_launch_facts("now", {"key_source": "local_owner_key", "observer": {"active": True}})
    assert "observer" not in pc._read_map_record("now")["launch_facts"]
    assert pc.last_local_launch_used_observer() is False


# ── what an older version left running or on disk ──────────────────────────


def _leftovers(start, pid: int) -> None:
    start._OBSERVER_STATE_DIR.mkdir(parents=True)
    (start._OBSERVER_STATE_DIR / "token").write_text("shim-token", encoding="utf-8")
    (start._OBSERVER_STATE_DIR / "observer.pid").write_text(str(pid), encoding="utf-8")
    (start._OBSERVER_STATE_DIR / "observer.log").write_text("log", encoding="utf-8")


def test_the_shim_token_is_deleted_once_the_shim_is_gone(start, monkeypatch):
    _leftovers(start, pid=424242)
    monkeypatch.setattr(start, "_pid_alive", lambda pid: False)
    start._local_llm_state("http://localhost:8011")
    assert not (start._OBSERVER_STATE_DIR / "token").exists()
    assert not (start._OBSERVER_STATE_DIR / "observer.pid").exists()
    assert (start._OBSERVER_STATE_DIR / "observer.log").exists()  # the user's log stays


def test_a_live_shim_keeps_its_token(start, monkeypatch):
    _leftovers(start, pid=424242)
    monkeypatch.setattr(start, "_pid_alive", lambda pid: True)
    start._local_llm_state("http://localhost:8011")
    assert (start._OBSERVER_STATE_DIR / "token").read_text(encoding="utf-8") == "shim-token"


def test_a_server_started_on_the_observer_is_left_alone_and_named(start, monkeypatch):
    pc = sys.modules["_plugin_common"]
    _leftovers(start, pid=424242)
    monkeypatch.setattr(start, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(pc, "live_server_record", lambda port: {"llm_observer": True})
    llm = start._local_llm_state("http://localhost:8011")
    assert llm["server_observer"] is True
    assert (start._OBSERVER_STATE_DIR / "token").exists()  # its shim may restart
    note = start._llm_note(llm)
    assert "started by an earlier plugin version on the Claude observer" in note
    assert "LLM API key option" in note


def test_no_leftovers_is_not_an_error(start):
    start._remove_observer_leftovers()
    assert not start._OBSERVER_STATE_DIR.exists()


# ── the status line's LLM sign ─────────────────────────────────────────────


@pytest.fixture
def watcher(suite, hook_module, isolated_modules, monkeypatch):
    if not suite.keys_from_plugin_options:
        pytest.skip(f"{suite.name}: never had the Claude observer")
    module = hook_module(suite, "idle-watcher.py")
    monkeypatch.setattr(module, "_log", lambda *a, **k: None)
    # Loaded after the watcher: its check imports this module when it runs.
    pc = isolated_modules(suite, "_plugin_common")
    verdicts: list = []
    monkeypatch.setattr(pc, "write_llm_state", lambda state, **k: verdicts.append(state))
    monkeypatch.setattr(pc, "clear_llm_state", lambda: verdicts.append("cleared"))
    monkeypatch.setattr(pc, "llm_key_owner", lambda config=None: "")
    monkeypatch.setattr(pc, "live_server_record", lambda port: {})
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_LLM_API_KEY", raising=False)
    module.pc = pc
    return module, verdicts


def test_the_watcher_marks_a_local_session_without_an_llm(watcher):
    module, verdicts = watcher
    module._check_llm_key({"base_url": "http://localhost:8011"})
    assert verdicts == ["not_set"]


def test_the_watcher_withdraws_the_mark_once_a_key_is_set(watcher, monkeypatch):
    module, verdicts = watcher
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_LLM_API_KEY", "sk-option")
    module._check_llm_key({"base_url": "http://localhost:8011"})
    assert verdicts == ["cleared"]


def test_the_watcher_says_nothing_about_a_remote_server(watcher):
    module, verdicts = watcher
    module._check_llm_key({"base_url": "https://tenant.cognee.ai"})
    assert verdicts == []


def test_the_watcher_does_not_mark_a_server_still_on_the_observer(watcher, monkeypatch):
    module, verdicts = watcher
    monkeypatch.setattr(module.pc, "live_server_record", lambda port: {"llm_observer": True})
    module._check_llm_key({"base_url": "http://localhost:8011"})
    assert verdicts == ["cleared"]
