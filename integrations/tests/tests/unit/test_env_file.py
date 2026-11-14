"""Unit tests for _env_file (one-time config via ~/.cognee/.env).

Covers:
  - dotenv parsing (comments, quotes, `export ` prefix, malformed lines)
  - setdefault precedence: a real env var always beats the file
  - denylist enforcement (PATH & friends never injected)
  - missing / unreadable file is a silent no-op
  - permission tightening to 0600 on load
  - first-run template creation (and that it never overwrites), Codex only
  - env_file_status diagnostics (names only, override detection)
  - Claude Code: credentials and connection settings only from the plugin
    options; the file supplies allowlisted COGNEE_* tuning names only

Migrated from {claude-code,codex}/tests/test_env_file.py (the two files were
byte-identical). The module's dir constants bind to the per-test temp HOME via
the isolated import, so the default-location tests need no constant patching.
"""

from __future__ import annotations

import os
import pathlib
import stat

import pytest


@pytest.fixture
def env_file_env(suite, isolated_modules, tmp_path, monkeypatch):
    """The isolated _env_file module plus a `fresh` helper for temp env files."""
    ef = isolated_modules(suite, "_env_file")

    def _fresh(content: str | None, name: str = ".env") -> pathlib.Path:
        """Point COGNEE_ENV_FILE at a fresh temp file and reset the loader."""
        path = tmp_path / name
        if path.exists():
            path.unlink()
        if content is not None:
            path.write_text(content, encoding="utf-8")
        monkeypatch.setenv("COGNEE_ENV_FILE", str(path))
        monkeypatch.setattr(ef, "_loaded", False)
        return path

    return ef, _fresh


def test_parse_basic_and_quotes(env_file_env):
    ef, fresh = env_file_env
    path = fresh(
        "\n".join(
            [
                "# a comment",
                "",
                "PLAIN=value",
                'DQ="double quoted"',
                "SQ='single quoted'",
                "SPACED =  padded value  ",
                "export EXPORTED=works",
                "EMPTY=",
            ]
        )
    )
    parsed = ef.parse_env_file(path)
    assert parsed["PLAIN"] == "value"
    assert parsed["DQ"] == "double quoted"
    assert parsed["SQ"] == "single quoted"
    assert parsed["SPACED"] == "padded value"
    assert parsed["EXPORTED"] == "works"
    assert parsed["EMPTY"] == ""


def test_parse_skips_malformed_lines(env_file_env):
    ef, fresh = env_file_env
    path = fresh(
        "\n".join(
            [
                "no_equals_sign_here",
                "BAD KEY=has space",
                "=novalue",
                "GOOD=1",
                "1NUM=still fine",  # isalnum allows leading digit; shells don't, but harmless
            ]
        )
    )
    parsed = ef.parse_env_file(path)
    assert "no_equals_sign_here" not in parsed
    assert "BAD KEY" not in parsed
    assert "" not in parsed
    assert parsed["GOOD"] == "1"


def test_load_sets_missing_and_respects_existing(env_file_env, monkeypatch):
    ef, fresh = env_file_env
    fresh("COGNEE_RECALL_TIMEOUT=7\nCOGNEE_REMEMBER_TIMEOUT=8\n")
    monkeypatch.setenv("COGNEE_REMEMBER_TIMEOUT", "99")
    monkeypatch.delenv("COGNEE_RECALL_TIMEOUT", raising=False)
    ef.load_env_file()
    assert os.environ.get("COGNEE_RECALL_TIMEOUT") == "7"
    assert os.environ.get("COGNEE_REMEMBER_TIMEOUT") == "99"
    monkeypatch.delenv("COGNEE_RECALL_TIMEOUT", raising=False)


def test_load_is_idempotent_per_process(env_file_env, monkeypatch):
    ef, fresh = env_file_env
    fresh("COGNEE_IDLE_POLL=first\n")
    monkeypatch.delenv("COGNEE_IDLE_POLL", raising=False)
    ef.load_env_file()
    assert os.environ.get("COGNEE_IDLE_POLL") == "first"
    monkeypatch.delenv("COGNEE_IDLE_POLL")
    # Second call without a reset must not re-read the file.
    ef.load_env_file()
    assert "COGNEE_IDLE_POLL" not in os.environ


def test_denylist_never_injected(env_file_env, monkeypatch):
    ef, fresh = env_file_env
    original_path = os.environ.get("PATH", "")
    fresh(
        "PATH=/evil\nLD_PRELOAD=/evil.so\nDYLD_INSERT_LIBRARIES=/evil.dylib\n"
        "PYTHONPATH=/evil\nCOGNEE_PLUGIN_VERBOSE=1\n"
    )
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    monkeypatch.delenv("DYLD_INSERT_LIBRARIES", raising=False)
    ef.load_env_file()
    assert os.environ.get("PATH", "") == original_path
    assert "LD_PRELOAD" not in os.environ
    assert "DYLD_INSERT_LIBRARIES" not in os.environ
    assert os.environ.get("COGNEE_PLUGIN_VERBOSE") == "1"
    monkeypatch.delenv("COGNEE_PLUGIN_VERBOSE", raising=False)


def test_parse_tolerates_utf8_bom_and_crlf(env_file_env):
    # Windows PowerShell 5.1 writes UTF-8 with a BOM and CRLF line endings;
    # the BOM must not glue onto the first key name.
    ef, fresh = env_file_env
    path = fresh(None)
    path.write_bytes(b'\xef\xbb\xbfCOGNEE_TEST_BOM="first"\r\nCOGNEE_TEST_SECOND=2\r\n')
    parsed = ef.parse_env_file(path)
    assert parsed["COGNEE_TEST_BOM"] == "first"
    assert parsed["COGNEE_TEST_SECOND"] == "2"


def test_parse_tolerates_utf16(env_file_env):
    # A PowerShell 5.1 `>` redirect (Out-File) writes UTF-16 LE with a BOM.
    ef, fresh = env_file_env
    path = fresh(None)
    path.write_text('COGNEE_TEST_U16="wide"\n', encoding="utf-16")
    parsed = ef.parse_env_file(path)
    assert parsed["COGNEE_TEST_U16"] == "wide"


def test_missing_file_is_noop(env_file_env):
    ef, fresh = env_file_env
    fresh(None)
    ef.load_env_file()  # must not raise


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions only")
def test_load_tightens_permissions(env_file_env, monkeypatch):
    ef, fresh = env_file_env
    path = fresh("COGNEE_PLUGIN_VERBOSE=1\n")
    path.chmod(0o644)
    ef.load_env_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    monkeypatch.delenv("COGNEE_PLUGIN_VERBOSE", raising=False)


def test_env_file_path_override_and_default(env_file_env, temp_home, monkeypatch):
    ef, fresh = env_file_env
    override = fresh("A=1\n")
    assert ef.env_file_path() == override
    monkeypatch.delenv("COGNEE_ENV_FILE")
    # The default resolves under the per-test HOME, never the real one.
    assert ef.env_file_path() == temp_home / ".cognee" / ".env"


def _needs_template(ef) -> None:
    # The Claude Code plugin writes no ~/.cognee/.env template (SDK-1411): the
    # file holds only tuning settings now, documented in the README.
    if not hasattr(ef, "ensure_env_file_template"):
        pytest.skip("this plugin writes no env-file template")


def test_template_not_written_when_override_set(env_file_env):
    ef, fresh = env_file_env
    _needs_template(ef)
    fresh(None)
    assert ef.ensure_env_file_template() is False


def test_template_never_overwrites(env_file_env, temp_home):
    # No COGNEE_ENV_FILE override: the default location (inside the isolated
    # temp HOME, bound at the isolated import) gets the template exactly once.
    ef, _ = env_file_env
    _needs_template(ef)
    assert ef.ensure_env_file_template() is True
    text = ef._DEFAULT_ENV_FILE.read_text(encoding="utf-8")
    assert str(ef._DEFAULT_ENV_FILE).startswith(str(temp_home))
    assert "COGNEE_BASE_URL" in text
    assert all(line.startswith("#") or not line for line in text.splitlines())
    if os.name != "nt":
        assert stat.S_IMODE(ef._DEFAULT_ENV_FILE.stat().st_mode) == 0o600
    ef._DEFAULT_ENV_FILE.write_text("USER_EDIT=kept\n", encoding="utf-8")
    assert ef.ensure_env_file_template() is False
    assert ef._DEFAULT_ENV_FILE.read_text(encoding="utf-8") == "USER_EDIT=kept\n"


def test_status_reports_names_not_values(env_file_env, suite, monkeypatch):
    ef, fresh = env_file_env
    fresh(
        "COGNEE_API_KEY=super-secret\nPATH=/evil\nCOGNEE_PLUGIN_DATASET=mine\n"
        "COGNEE_RECALL_TIMEOUT=5\n"
    )
    monkeypatch.setenv("COGNEE_RECALL_TIMEOUT", "9")
    ef.load_env_file()
    status = ef.env_file_status()
    assert status["exists"] is True
    assert "COGNEE_PLUGIN_DATASET" in status["keys"]
    assert "super-secret" not in str(status)
    assert status["overridden"] == ["COGNEE_RECALL_TIMEOUT"]
    if suite.keys_from_plugin_options:
        assert status["refused"] == ["COGNEE_API_KEY"]
        assert status["ignored"] == ["PATH"]
        assert "COGNEE_API_KEY" not in status["keys"]
    else:
        assert status["blocked"] == ["PATH"]
    monkeypatch.delenv("COGNEE_PLUGIN_DATASET", raising=False)


def test_status_missing_file(env_file_env):
    ef, fresh = env_file_env
    path = fresh(None)
    status = ef.env_file_status()
    assert status == {"path": str(path), "exists": False}


# ── the fix dialog's write: COGNEE_ENV_FILE honoured, owner-only ────────────


def _needs_env_append(ef) -> None:
    # The Claude Code plugin writes no key anywhere (SDK-1411), so it has no append.
    if not hasattr(ef, "append_env_value"):
        pytest.skip("this plugin never writes a value to the env file")


def test_append_creates_the_file_owner_only_where_the_hooks_read(env_file_env, monkeypatch):
    ef, fresh = env_file_env
    _needs_env_append(ef)
    path = fresh(None, name="custom.env")  # COGNEE_ENV_FILE points here; file absent
    written = ef.append_env_value("COGNEE_API_KEY", "ck_live_123")
    assert written == path
    assert path.read_text(encoding="utf-8") == 'COGNEE_API_KEY="ck_live_123"\n'
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_append_keeps_existing_lines_and_ends_them(env_file_env):
    ef, fresh = env_file_env
    _needs_env_append(ef)
    path = fresh('COGNEE_BASE_URL="https://x"')  # no trailing newline
    ef.append_env_value("COGNEE_API_KEY", "k")
    assert path.read_text(encoding="utf-8") == 'COGNEE_BASE_URL="https://x"\nCOGNEE_API_KEY="k"\n'


@pytest.mark.parametrize("name, value", [("bad name", "v"), ("COGNEE_API_KEY", "two\nlines")])
def test_append_refuses_a_bad_name_or_a_multiline_value(env_file_env, name, value):
    ef, fresh = env_file_env
    _needs_env_append(ef)
    path = fresh(None)
    with pytest.raises(ValueError):
        ef.append_env_value(name, value)
    assert not path.exists()


# ── Claude Code: credentials only from the plugin options (SDK-1411) ────────


@pytest.fixture
def options_only(suite):
    if not suite.keys_from_plugin_options:
        pytest.skip(f"{suite.name}: reads its settings from the environment and the file")


_LEGACY_FILE = (
    'COGNEE_BASE_URL="https://tenant.cognee.ai"\n'
    "COGNEE_API_KEY=ck_file_secret\n"
    "OPENAI_API_KEY=sk-file-secret\n"
    "LLM_API_KEY=sk-llm-file-secret\n"
    "GRAPH_DATABASE_PASSWORD=pw-file-secret\n"
    "COGNEE_USER_PASSWORD=user-file-secret\n"
    "COGNEE_RECALL_TIMEOUT=7\n"
)
_FILE_SECRETS = (
    "ck_file_secret",
    "sk-file-secret",
    "sk-llm-file-secret",
    "pw-file-secret",
    "user-file-secret",
    "tenant.cognee.ai",
)


def test_only_allowlisted_tuning_names_reach_the_environment(
    options_only, env_file_env, monkeypatch
):
    ef, fresh = env_file_env
    for name in ("COGNEE_BASE_URL", "COGNEE_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("GRAPH_DATABASE_PASSWORD", raising=False)
    monkeypatch.delenv("COGNEE_RECALL_TIMEOUT", raising=False)
    path = fresh(_LEGACY_FILE)
    ef.load_env_file()

    assert os.environ.get("COGNEE_RECALL_TIMEOUT") == "7"
    for name in (
        "COGNEE_BASE_URL",
        "COGNEE_API_KEY",
        "OPENAI_API_KEY",
        "LLM_API_KEY",
        "GRAPH_DATABASE_PASSWORD",
        "COGNEE_USER_PASSWORD",
    ):
        assert name not in os.environ, name
    # Refused values are dropped while parsing: no structure the module keeps
    # or returns holds them, only their names.
    held = repr(
        (
            ef.parse_env_file(path),
            ef.env_file_status(),
            ef.refused_names_in_file(path),
            ef._injected,
            vars(ef).get("_seen"),
        )
    )
    for secret in _FILE_SECRETS:
        assert secret not in held, secret
    assert "COGNEE_API_KEY" in ef.refused_names_in_file(path)
    monkeypatch.delenv("COGNEE_RECALL_TIMEOUT", raising=False)


@pytest.mark.parametrize(
    "name",
    [
        "COGNEE_BASE_URL",
        "COGNEE_API_KEY",
        "COGNEE_PRINCIPAL_API_KEY",
        "COGNEE_USER_EMAIL",
        "LLM_MODEL",
        "EMBEDDING_PROVIDER",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "NEO4J_PASSWORD",
        "SOME_CREDENTIALS_FILE",
        "PROXY_AUTH",
    ],
)
def test_credential_and_connection_names_are_refused(options_only, env_file_env, name):
    ef, _ = env_file_env
    assert ef.refused(name)


def test_a_shell_url_and_keys_are_replaced_by_the_options(options_only, env_file_env, monkeypatch):
    ef, fresh = env_file_env
    fresh(None)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugin")
    monkeypatch.setenv("COGNEE_BASE_URL", "https://shell.example")
    monkeypatch.setenv("COGNEE_API_KEY", "shell-key")
    monkeypatch.setenv("COGNEE_PRINCIPAL_API_KEY", "shell-principal")
    monkeypatch.setenv("LLM_API_KEY", "shell-llm")
    monkeypatch.setenv("LLM_ENDPOINT", "https://shell-llm.example")
    monkeypatch.setenv("COGNEE_USER_PASSWORD", "shell-pw")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_LLM_API_KEY", "option-llm")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_LLM_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_EMBEDDING_DIMENSIONS", "3072")
    monkeypatch.delenv(ef._LLM_ENV_APPLIED, raising=False)
    ef.load_env_file()

    assert "COGNEE_BASE_URL" not in os.environ  # no URL option: local
    assert "COGNEE_API_KEY" not in os.environ
    assert "COGNEE_PRINCIPAL_API_KEY" not in os.environ
    assert "COGNEE_USER_PASSWORD" not in os.environ
    assert "LLM_ENDPOINT" not in os.environ
    assert os.environ["LLM_API_KEY"] == "option-llm"
    assert os.environ["LLM_MODEL"] == "openai/gpt-4o-mini"
    assert os.environ["EMBEDDING_DIMENSIONS"] == "3072"


def test_url_and_key_options_select_cloud(options_only, env_file_env, monkeypatch):
    ef, fresh = env_file_env
    fresh(None)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugin")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_COGNEE_BASE_URL", "https://tenant.cognee.ai")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_COGNEE_API_KEY", "ck_option")
    monkeypatch.setenv("COGNEE_BASE_URL", "https://shell.example")
    ef.load_env_file()
    assert os.environ["COGNEE_BASE_URL"] == "https://tenant.cognee.ai"
    assert os.environ["COGNEE_API_KEY"] == "ck_option"


def test_a_child_keeps_what_its_parent_applied(options_only, env_file_env, monkeypatch):
    """The observer's LLM settings, applied by SessionStart, reach its workers."""
    ef, fresh = env_file_env
    fresh(None)
    monkeypatch.setenv(ef._LLM_ENV_APPLIED, "1")
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_API_KEY", "observer-token")
    monkeypatch.setenv("COGNEE_API_KEY", "parent-owner-key")
    ef.load_env_file()
    assert os.environ["LLM_PROVIDER"] == "custom"
    assert os.environ["LLM_API_KEY"] == "observer-token"
    # A key is never handed down through the environment.
    assert "COGNEE_API_KEY" not in os.environ


@pytest.mark.parametrize(
    "url, honoured",
    [
        ("http://127.0.0.1:9000", True),
        ("http://localhost:8011", True),
        ("https://evil.example", False),
    ],
)
def test_local_api_url_is_honoured_only_on_loopback(
    options_only, env_file_env, monkeypatch, url, honoured
):
    ef, fresh = env_file_env
    monkeypatch.delenv("COGNEE_LOCAL_API_URL", raising=False)
    fresh(f"COGNEE_LOCAL_API_URL={url}\n")
    ef.load_env_file()
    assert (os.environ.get("COGNEE_LOCAL_API_URL") == url) is honoured
    monkeypatch.delenv("COGNEE_LOCAL_API_URL", raising=False)
    monkeypatch.setattr(ef, "_loaded", False)
    fresh(None)
    monkeypatch.setenv("COGNEE_LOCAL_API_URL", url)
    ef.load_env_file()
    assert (os.environ.get("COGNEE_LOCAL_API_URL") == url) is honoured


def test_legacy_settings_are_named_only_while_the_options_are_empty(
    options_only, env_file_env, monkeypatch
):
    ef, fresh = env_file_env
    fresh(_LEGACY_FILE)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugin")
    legacy = ef.legacy_settings_in_file()
    assert legacy == {"cloud": ["COGNEE_API_KEY", "COGNEE_BASE_URL"], "llm": ["LLM_API_KEY"]}
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_COGNEE_BASE_URL", "https://tenant.cognee.ai")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_LLM_MODEL", "openai/gpt-4o-mini")
    assert ef.legacy_settings_in_file() == {"cloud": [], "llm": []}


def test_no_template_writer(options_only, env_file_env):
    ef, _ = env_file_env
    assert not hasattr(ef, "ensure_env_file_template")
