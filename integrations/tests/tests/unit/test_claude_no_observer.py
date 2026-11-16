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

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import pytest


@pytest.fixture
def start(suite, hook_module, monkeypatch, tmp_path):
    if not suite.keys_from_plugin_options:
        pytest.skip(f"{suite.name}: never had the Claude observer")
    module = hook_module(suite, "session-start.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(module, "_EMBEDDER_PIN", tmp_path / "state" / "embedder.json")
    # The server is the plugin's own unless a test says otherwise (no probe).
    monkeypatch.setattr(sys.modules["_plugin_common"], "llm_key_owner", lambda config=None: "")
    for name in ("EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS", "LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_LLM_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_EMBEDDING_API_KEY", raising=False)
    return module


def _old_launch(pc, key: str, *, observer_active: bool, key_source: str, age: float = 0) -> None:
    """A launch record as 1.8.3 wrote it, ``observer`` fact included."""
    pc._write_map_record(key, {"session_id": key, "conn_uuid": "c", "cwd": "/p"})
    pc.record_launch_facts(key, {"key_source": key_source})
    record = pc._read_map_record(key)
    record["launch_facts"]["observer"] = {"active": observer_active, "reason": "", "model": ""}
    record["launch_facts"]["recorded_at"] = time.time() - age
    pc._write_map_record(key, record)


def _legacy_launch(
    pc, key: str, *, observer_active: bool, age: float = 0, base_url: str = "http://localhost:8011"
) -> None:
    """A launch record as 1.6.0 to 1.8.1 wrote it: no ``launch_facts``, the
    observer on a top-level ``llm_observer``, stamped by ``created_at``."""
    created = datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat(timespec="seconds")
    pc._write_map_record(
        key,
        {
            "session_id": key,
            "host_key": key,
            "created_at": created,
            "base_url": base_url,
            "llm_observer": {"active": observer_active, "model": "haiku", "endpoint": ""},
        },
    )


# ── nothing shipped runs or names the observer ─────────────────────────────


def test_no_shipped_file_runs_claude_or_names_the_shim(suite):
    if not suite.keys_from_plugin_options:
        pytest.skip(f"{suite.name}: never had the Claude observer")
    scripts = suite.scripts_dir
    assert not (scripts / "_observer.py").exists()
    assert not (scripts / "claude-observer.py").exists()
    pattern = re.compile(
        r"claude-observer|observer_token|observer\.pid|\.credentials\.json|keychain|"
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
    assert llm == {
        "configured": False,
        "provider_without_key": "",
        "embedder_pinned": False,
        "server_observer": False,
        "server_owner": "",
    }
    note = start._llm_note(llm)
    assert "local mode has no LLM" in note
    assert "LLM API key option" in note
    assert "fastembed" not in note
    output = start._with_llm_note({"systemMessage": "Cognee Memory ready"}, llm)
    assert output["systemMessage"].startswith("Cognee Memory ready\n\n")
    assert "local mode has no LLM" in output["systemMessage"]


def test_a_configured_llm_gets_no_note(start, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_LLM_API_KEY", "sk-option")
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
    "name, value",
    [
        ("EMBEDDING_MODEL", "openai/text-embedding-3-large"),
        ("EMBEDDING_DIMENSIONS", "3072"),
        ("CLAUDE_PLUGIN_OPTION_EMBEDDING_API_KEY", "sk-embed"),
    ],
    ids=["model only", "dimensions only", "embedding key only"],
)
def test_any_embedding_option_replaces_the_kept_embedder_as_a_whole(
    start, monkeypatch, name, value
):
    """fastembed with the user's model name or dimensions cannot embed."""
    pc = sys.modules["_plugin_common"]
    _old_launch(pc, "old-local", observer_active=True, key_source="local_owner_key")
    monkeypatch.setenv(name, value)  # as the option is applied
    assert start._apply_embedder_pin() is False
    assert "EMBEDDING_PROVIDER" not in os.environ
    for pinned in ("EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS"):
        if pinned != name:
            assert pinned not in os.environ


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


def test_an_observer_install_from_before_launch_facts_is_pinned(start):
    """1.6.0 to 1.8.1 recorded the observer without ``launch_facts``."""
    pc = sys.modules["_plugin_common"]
    _legacy_launch(pc, "old-local", observer_active=True)
    assert pc.last_local_launch_used_observer() is True
    assert start._apply_embedder_pin() is True
    assert os.environ["EMBEDDING_PROVIDER"] == "fastembed"
    assert start._EMBEDDER_PIN.exists()


def test_a_legacy_install_that_moved_off_the_observer_is_not_pinned(start):
    pc = sys.modules["_plugin_common"]
    _legacy_launch(pc, "older", observer_active=True, age=3600)
    _legacy_launch(pc, "newer", observer_active=False, age=60)
    assert start._apply_embedder_pin() is False
    assert not start._EMBEDDER_PIN.exists()


def test_a_legacy_cloud_launch_does_not_hide_the_observer_install(start):
    pc = sys.modules["_plugin_common"]
    _legacy_launch(pc, "local", observer_active=True, age=3600)
    _legacy_launch(
        pc, "cloud", observer_active=False, age=60, base_url="https://tenant.cognee.example"
    )
    assert pc.last_local_launch_used_observer() is True


def test_a_newer_launch_on_a_key_outranks_a_legacy_observer_record(start):
    """Both shapes are ordered on one clock: 1.8.1 on the observer, then 1.8.3 on a key."""
    pc = sys.modules["_plugin_common"]
    _legacy_launch(pc, "legacy", observer_active=True, age=3600)
    _old_launch(pc, "later", observer_active=False, key_source="local_owner_key", age=60)
    assert pc.last_local_launch_used_observer() is False


def test_an_unreadable_record_does_not_hide_the_observer_install(start):
    pc = sys.modules["_plugin_common"]
    _legacy_launch(pc, "old-local", observer_active=True)
    _old_launch(pc, "broken", observer_active=False, key_source="local_owner_key")
    record = pc._read_map_record("broken")
    record["launch_facts"]["recorded_at"] = "not a number"
    pc._write_map_record("broken", record)
    assert pc.last_local_launch_used_observer() is True


def test_this_version_records_no_observer_fact(start):
    pc = sys.modules["_plugin_common"]
    pc._write_map_record("now", {"session_id": "s", "conn_uuid": "c", "cwd": "/p"})
    pc.record_launch_facts("now", {"key_source": "local_owner_key", "observer": {"active": True}})
    assert "observer" not in pc._read_map_record("now")["launch_facts"]
    assert pc.last_local_launch_used_observer() is False


# ── a server an older version started on the observer ──────────────────────


def test_a_server_started_on_the_observer_is_named(start, monkeypatch):
    pc = sys.modules["_plugin_common"]
    monkeypatch.setattr(pc, "live_server_record", lambda port: {"llm_observer": True})
    llm = start._local_llm_state("http://localhost:8011")
    assert llm["server_observer"] is True
    note = start._llm_note(llm)
    assert "started by an earlier plugin version on the Claude observer" in note
    assert "LLM API key option" in note


def test_session_start_has_no_code_for_the_shim_files(start):
    """This version neither reads nor deletes what the removed shim left."""
    assert not hasattr(start, "_remove_observer_leftovers")
    assert not hasattr(start, "_OBSERVER_STATE_DIR")


# ── a provider without the key it needs ────────────────────────────────────


def test_a_provider_without_a_key_is_not_an_llm(start, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    llm = start._local_llm_state("http://localhost:8011")
    assert llm["configured"] is False
    note = start._llm_note(llm)
    assert "LLM provider option is set to anthropic but the LLM API key option is empty" in note


@pytest.mark.parametrize("provider", ["bedrock", "llama_cpp", "Bedrock"])
def test_a_provider_that_needs_no_key_is_an_llm(start, monkeypatch, provider):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    llm = start._local_llm_state("http://localhost:8011")
    assert llm["configured"] is True
    assert start._llm_note(llm) == ""


# ── a local server the plugin did not start ────────────────────────────────


@pytest.mark.parametrize("owner", ["managed_endpoint", "unowned_server"])
def test_a_server_the_plugin_did_not_start_gets_no_note_and_no_pin(start, monkeypatch, owner):
    """It has its own LLM and embedder: the options say nothing about it."""
    pc = sys.modules["_plugin_common"]
    _old_launch(pc, "old-local", observer_active=True, key_source="local_owner_key")
    monkeypatch.setattr(pc, "llm_key_owner", lambda config=None: owner)
    llm = start._local_llm_state("http://localhost:8011", {"base_url": "http://localhost:8011"})
    assert llm["server_owner"] == owner
    assert llm["embedder_pinned"] is False
    assert start._llm_note(llm) == ""
    assert "EMBEDDING_PROVIDER" not in os.environ
    assert not start._EMBEDDER_PIN.exists()


# ── nothing is installed in the observer's place ───────────────────────────


def test_a_server_without_an_llm_may_not_install_the_local_extractor(start, monkeypatch):
    assert start._server_env()["GLINER_AUTO_INSTALL"] == "false"
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")  # a provider alone is no LLM
    assert start._server_env()["GLINER_AUTO_INSTALL"] == "false"


@pytest.mark.parametrize(
    "env",
    [{"CLAUDE_PLUGIN_OPTION_LLM_API_KEY": "sk-option"}, {"LLM_PROVIDER": "bedrock"}],
    ids=["key", "provider that needs none"],
)
def test_a_server_with_an_llm_keeps_cognees_default(start, monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert "GLINER_AUTO_INSTALL" not in start._server_env()


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
    _recorded(module, monkeypatch, key=False)
    return module, verdicts


def _recorded(module, monkeypatch, *, key: bool | None, provider: str = "") -> None:
    """What session start recorded about the LLM options for this launch
    (``key=None``: it recorded nothing)."""
    facts = None if key is None else {"llm_key_option_set": key, "llm_provider": provider}
    monkeypatch.setattr(module.pc, "_read_map_record", lambda host_key: {"launch_facts": facts})


def test_the_watcher_marks_a_local_session_without_an_llm(watcher):
    module, verdicts = watcher
    module._check_llm_key({"base_url": "http://localhost:8011"})
    assert verdicts == ["not_set"]


def test_the_watcher_withdraws_the_mark_once_a_key_is_set(watcher, monkeypatch):
    module, verdicts = watcher
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_LLM_API_KEY", "sk-option")
    module._check_llm_key({"base_url": "http://localhost:8011"})
    assert verdicts == ["cleared"]


def test_a_watcher_without_options_trusts_what_session_start_recorded(watcher, monkeypatch):
    """The watcher a dataset switch restarts is given no options: a key that
    session start saw must not turn into a "no LLM" mark."""
    module, verdicts = watcher
    _recorded(module, monkeypatch, key=True)
    module._check_llm_key({"base_url": "http://localhost:8011"})
    assert verdicts == ["cleared"]


def test_a_watcher_that_knows_nothing_gives_no_verdict(watcher, monkeypatch):
    module, verdicts = watcher
    _recorded(module, monkeypatch, key=None)
    module._check_llm_key({"base_url": "http://localhost:8011"})
    assert verdicts == []


def test_the_watcher_marks_a_provider_without_its_key(watcher, monkeypatch):
    module, verdicts = watcher
    _recorded(module, monkeypatch, key=False, provider="anthropic")
    module._check_llm_key({"base_url": "http://localhost:8011"})
    assert verdicts == ["not_set"]
    verdicts.clear()
    _recorded(module, monkeypatch, key=False, provider="bedrock")
    module._check_llm_key({"base_url": "http://localhost:8011"})
    assert verdicts == ["cleared"]


def test_the_watcher_renews_the_mark_while_the_session_runs(
    suite, watcher, isolated_modules, monkeypatch, tmp_path
):
    """The status line drops a verdict older than its TTL, so one check at
    start would let a long session without an LLM go back to the plain dot."""
    module, _ = watcher
    bar = isolated_modules(suite, "cognee_statusline_render")
    assert module.LLM_CHECK_SECONDS < bar._LLM_STATE_STALE_SECONDS

    checks: list = []

    def check(config):
        checks.append(config)
        if len(checks) == 3:
            module._should_stop = True

    monkeypatch.setattr(module, "_check_llm_key", check)
    monkeypatch.setattr(module, "LLM_CHECK_SECONDS", 0.0)
    monkeypatch.setattr(module, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(module, "SHARED_REFRESH_SECONDS", 0.0)
    monkeypatch.setattr(module, "_STOPFILE", tmp_path / "watcher.stop")
    monkeypatch.setattr(module, "_PIDFILE", tmp_path / "watcher.pid")
    monkeypatch.setattr(module, "_owns_pidfile", lambda: True)
    monkeypatch.setattr(module, "_should_stop", False)
    asyncio.run(module._main_loop("s", "d", {"base_url": "http://localhost:8011"}))
    assert len(checks) == 3


def test_the_watcher_says_nothing_about_a_remote_server(watcher):
    module, verdicts = watcher
    module._check_llm_key({"base_url": "https://tenant.cognee.ai"})
    assert verdicts == []


def test_the_watcher_does_not_mark_a_server_still_on_the_observer(watcher, monkeypatch):
    module, verdicts = watcher
    monkeypatch.setattr(module.pc, "live_server_record", lambda port: {"llm_observer": True})
    module._check_llm_key({"base_url": "http://localhost:8011"})
    assert verdicts == ["cleared"]
