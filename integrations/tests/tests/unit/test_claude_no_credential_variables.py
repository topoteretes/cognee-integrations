"""No credential travels through an environment variable (SDK-1411, Claude Code).

The plugin directory holds a plugin that reads a credential from the user's
environment or files and could send it to a server. Every key this plugin uses
is a plugin option (``userConfig``, ``sensitive: true``), which Claude Code
hands to hook processes as ``CLAUDE_PLUGIN_OPTION_<KEY>``. Until 1.8.3 the
plugin copied each option into a credential-named variable
(``COGNEE_API_KEY``, ``LLM_API_KEY``, ``EMBEDDING_API_KEY``) and read it back
from there in a dozen scripts, which is indistinguishable from reading the
user's shell. Now:

- no shipped file reads a credential-named variable (a static scan);
- the Cloud key is read from its option where a request needs it, and no
  resolver writes a key into the environment;
- the LLM and embedding keys reach only the environment of the server the
  plugin starts;
- ``cognee-search.sh --category`` replaces the hand-written ``curl`` the skill
  texts used for a category search.
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys

import pytest
from utils.isolation import build_env, usable_bash

BASH = usable_bash()

_NAME = r"[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)[A-Z0-9_]*"
#: A read of a credential-named variable in Python: get, getenv, pop, or a
#: subscript that is not an assignment.
_PY_READS = (
    re.compile(r"environ\.get\(\s*[\"'](%s)[\"']" % _NAME),
    re.compile(r"getenv\(\s*[\"'](%s)[\"']" % _NAME),
    re.compile(r"environ\.pop\(\s*[\"'](%s)[\"']" % _NAME),
    re.compile(r"environ\[\s*[\"'](%s)[\"']\s*\](?!\s*=(?!=))" % _NAME),
)
#: A shell expansion of one, in a script, a hook command or a skill text.
_SHELL_READ = re.compile(r"\$\{?(%s)\b" % _NAME)
_TEXT_SUFFIXES = {".py", ".sh", ".md", ".json", ".yaml", ".yml", ".txt", ""}


@pytest.fixture
def claude(suite):
    if not suite.keys_from_plugin_options:
        pytest.skip(f"{suite.name}: reads its keys from the environment and ~/.cognee/.env")
    return suite


def _own_the_environment(monkeypatch) -> None:
    """Scrub the inherited plugin variables once and keep what the test sets
    afterwards: the module loaders scrub again on every load, which would
    remove the options a test sets before importing a module."""
    from utils import isolation

    isolation.scrub_cognee_env(monkeypatch)
    monkeypatch.setattr(isolation, "scrub_cognee_env", lambda _monkeypatch: None)


def _credential_reads(path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    patterns = _PY_READS if path.suffix == ".py" else (*_PY_READS, _SHELL_READ)
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for pattern in patterns:
            found += [f"{path.name}:{number}: {m.group(1)}" for m in pattern.finditer(line)]
    return found


def _shipped_files(plugin):
    for path in sorted(plugin.rglob("*")):
        # No file is exempt: the plugin directory reads the release history
        # like any other file, so it names old variables in prose only.
        if not path.is_file():
            continue
        if {"node_modules", "__pycache__", ".git"} & set(path.parts):
            continue
        if path.suffix in _TEXT_SUFFIXES:
            yield path


def test_no_shipped_file_reads_a_credential_named_variable(claude):
    plugin = claude.scripts_dir.parent
    offenders = [hit for path in _shipped_files(plugin) for hit in _credential_reads(path)]
    assert offenders == []


def test_the_scan_catches_each_form(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text(
        'a = os.environ.get("COGNEE_API_KEY")\n'
        'b = os.getenv("NOTION_TOKEN")\n'
        'c = os.environ["LLM_API_KEY"]\n'
        'd = os.environ.pop("DB_PASSWORD", None)\n'
        'env["LLM_API_KEY"] = value  # a child environment: an assignment\n'
        'os.environ["HASH_API_KEY"] = "false"\n'
        'e = os.environ.get("COGNEE_SESSION_KEY")  # not a credential name\n'
        'f = os.environ.get("CLAUDE_PLUGIN_OPTION_" + name.upper())\n',
        encoding="utf-8",
    )
    assert [hit.split(": ")[1] for hit in _credential_reads(sample)] == [
        "COGNEE_API_KEY",
        "NOTION_TOKEN",
        "LLM_API_KEY",
        "DB_PASSWORD",
    ]
    shell = tmp_path / "sample.md"
    shell.write_text('curl -H "X-Api-Key: ${COGNEE_API_KEY}" "$COGNEE_BASE_URL"\n', "utf-8")
    assert [hit.split(": ")[1] for hit in _credential_reads(shell)] == ["COGNEE_API_KEY"]


# ── the Cloud key: read from the option, never written to the environment ──


def test_resolving_the_cloud_key_writes_none_into_the_environment(
    claude, isolated_modules, monkeypatch
):
    """The resolvers return the key and leave no copy of it in a variable a
    child process (or a shell command) could read back."""
    _own_the_environment(monkeypatch)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugin")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_COGNEE_BASE_URL", "https://tenant.cognee.ai")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_COGNEE_API_KEY", "ck_option")
    monkeypatch.setenv("COGNEE_API_KEY", "ck_shell")
    pc = isolated_modules(claude, "_plugin_common")
    config = importlib.import_module("config")

    assert pc.resolved_http_endpoint_auth() == ("https://tenant.cognee.ai", "ck_option")
    assert pc.resolve_runtime_mode()["api_key_present"] is True
    assert config.load_config()["api_key"] == "ck_option"
    assert pc.principal_key_for_control_plane() == "ck_option"
    assert "ck_option" not in [v for k, v in os.environ.items() if "OPTION" not in k]
    assert "ck_shell" not in os.environ.values()


def test_resolving_the_local_owner_key_writes_none_into_the_environment(
    claude, isolated_modules, mock_server, monkeypatch
):
    """A local server: its own owner key, looked up from it, never the Cloud
    key option, and nowhere in the environment afterwards."""
    _own_the_environment(monkeypatch)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugin")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_COGNEE_API_KEY", "ck_option")
    monkeypatch.setenv("COGNEE_LOCAL_API_URL", mock_server.url)
    pc = isolated_modules(claude, "_plugin_common")
    owner = mock_server.identity.seed_owner_key("default_user@example.com")

    assert pc.resolved_http_endpoint_auth() == (mock_server.url, owner)
    assert pc.resolve_runtime_mode()["key_source"] == "local_owner_key"
    assert owner not in os.environ.values()
    assert "COGNEE_API_KEY" not in os.environ


def test_a_process_without_options_has_no_cloud_key(claude, isolated_modules, monkeypatch):
    """A Bash-run skill: the shell's own variable is not a source of the key."""
    _own_the_environment(monkeypatch)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_COGNEE_API_KEY", raising=False)
    monkeypatch.setenv("COGNEE_API_KEY", "ck_shell")
    monkeypatch.setenv("COGNEE_PRINCIPAL_API_KEY", "ck_shell_principal")
    pc = isolated_modules(claude, "_plugin_common")
    ef = sys.modules["_env_file"]
    assert ef.cloud_key("https://tenant.cognee.ai") == ""
    assert pc._api_key_with_source("https://tenant.cognee.ai") == ("", "missing")
    assert "COGNEE_API_KEY" not in os.environ
    assert "COGNEE_PRINCIPAL_API_KEY" not in os.environ


# ── the LLM and embedding keys: the started server's environment only ──────


def _load_options(monkeypatch, **options) -> None:
    ef = sys.modules["_env_file"]
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugin")
    for name, value in options.items():
        monkeypatch.setenv(f"CLAUDE_PLUGIN_OPTION_{name.upper()}", value)
    for name in list(os.environ):
        if name.startswith(("LLM_", "EMBEDDING_")):
            monkeypatch.setenv(name, os.environ[name])  # restored at teardown
    monkeypatch.delenv(ef._LLM_ENV_APPLIED, raising=False)
    monkeypatch.setattr(ef, "_loaded", False)
    ef.load_env_file()


def test_the_llm_keys_reach_the_server_env_and_not_the_plugin_process(
    claude, hook_module, monkeypatch
):
    start = hook_module(claude, "session-start.py")
    monkeypatch.setenv("LLM_API_KEY", "sk-shell")
    _load_options(monkeypatch, llm_api_key="sk-option-llm", embedding_api_key="sk-option-embed")

    assert "LLM_API_KEY" not in os.environ
    assert "EMBEDDING_API_KEY" not in os.environ
    env = start._server_env()
    assert env["LLM_API_KEY"] == "sk-option-llm"
    assert env["EMBEDDING_API_KEY"] == "sk-option-embed"


def test_a_key_left_in_the_process_never_reaches_the_server(claude, hook_module, monkeypatch):
    """A key-named variable still in this process (an older plugin version's
    ancestor, a shell export that survived) is not copied to the server: the
    server's keys come from the options only."""
    start = hook_module(claude, "session-start.py")
    _load_options(monkeypatch, llm_api_key="sk-option-llm")
    monkeypatch.setenv("LLM_API_KEY", "sk-stale")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-stale-embed")
    env = start._server_env()
    assert env["LLM_API_KEY"] == "sk-option-llm"
    assert "EMBEDDING_API_KEY" not in env


def test_without_a_key_option_the_server_gets_none(claude, hook_module, monkeypatch):
    start = hook_module(claude, "session-start.py")
    monkeypatch.setenv("LLM_API_KEY", "sk-shell")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-shell-embed")
    _load_options(monkeypatch)
    env = start._server_env()
    assert "LLM_API_KEY" not in env
    assert "EMBEDDING_API_KEY" not in env


# ── the category search the skills used a hand-written curl for ────────────


def _search(suite, home, mock_server, *args):
    return subprocess.run(
        [BASH, str(suite.scripts_dir / "cognee-search.sh"), *args],
        env=build_env(suite, home, service_url=mock_server.url, api_key="k"),
        cwd=str(home),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


@pytest.mark.skipif(BASH is None, reason="requires a working POSIX bash")
def test_search_category_sends_the_node_set_filter(claude, temp_home, mock_server):
    mock_server.set_recall_results([{"source": "graph", "content": "a project doc"}])
    result = _search(
        claude, temp_home, mock_server, "deploy steps", "5", "--category", "project_docs"
    )
    assert result.returncode == 0, result.stderr
    body = mock_server.assert_called("POST", "/api/v1/recall")["json"]
    assert body["node_name"] == ["project_docs"]
    assert body["node_name_filter_operator"] == "OR"
    assert body["scope"] == ["graph"]


@pytest.mark.skipif(BASH is None, reason="requires a working POSIX bash")
@pytest.mark.parametrize(
    "args", [("--category", "everything"), ("--code", "--category", "project_docs")]
)
def test_search_refuses_an_unknown_or_code_category(claude, temp_home, mock_server, args):
    result = _search(claude, temp_home, mock_server, "deploy steps", "5", *args)
    assert result.returncode == 1
    assert "--category takes one of" in result.stderr
    assert all(c["path"] != "/api/v1/recall" for c in mock_server.calls)


@pytest.mark.skipif(BASH is None, reason="requires a working POSIX bash")
def test_a_plain_search_sends_no_category(claude, temp_home, mock_server):
    mock_server.set_recall_results([])
    result = _search(claude, temp_home, mock_server, "deploy steps", "5")
    assert result.returncode == 0, result.stderr
    assert "node_name" not in mock_server.assert_called("POST", "/api/v1/recall")["json"]


# ── the shell scripts: no key is held, sent or passed on a command line ────


def test_no_shipped_shell_script_holds_or_sends_a_key(claude):
    """Every keyed request is made by a Python script, in one process. A shell
    script that built the header itself kept the key in a shell variable and
    put it on a command line (``curl -H``, a ``python3`` argument), where
    ``ps`` shows it."""
    scripts = sorted(claude.scripts_dir.glob("*.sh"))
    assert scripts, "no shell script found"
    pattern = re.compile(r"X-Api-Key|Authorization:|API_KEY|\bcurl\b|\bwget\b")
    offenders = [
        f"{path.name}:{number}: {line.strip()[:100]}"
        for path in scripts
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []


def test_the_code_graph_command_authorizes_its_own_request(claude, hook_module, monkeypatch):
    """``cognee-index-repo.sh`` passes no key: an empty one is resolved in the
    process that sends the request, and a given one is used as it is."""
    graph = hook_module(claude, "_code_graph.py")
    seen = []
    monkeypatch.setattr(graph, "_resolved_auth", lambda url: f"owner-of:{url}")
    monkeypatch.setattr(
        graph,
        "index_repo",
        lambda url, key, spec, dataset, **kwargs: seen.append((url, key)) or {"ok": True},
    )
    graph.main(["http://localhost:8011", "", "index", "/repo"])
    graph.main(["http://localhost:8011", "given", "index", "/repo"])
    assert seen == [
        ("http://localhost:8011", "owner-of:http://localhost:8011"),
        ("http://localhost:8011", "given"),
    ]
