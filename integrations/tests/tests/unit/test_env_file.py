"""Unit tests for _env_file (one-time config via ~/.cognee/.env).

Covers:
  - dotenv parsing (comments, quotes, `export ` prefix, malformed lines)
  - setdefault precedence: a real env var always beats the file
  - denylist enforcement (PATH & friends never injected)
  - missing / unreadable file is a silent no-op
  - permission tightening to 0600 on load
  - first-run template creation (and that it never overwrites)
  - env_file_status diagnostics (names only, override detection)

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
    fresh("COGNEE_TEST_FROM_FILE=file-value\nCOGNEE_TEST_EXPORTED=file-value\n")
    monkeypatch.setenv("COGNEE_TEST_EXPORTED", "shell-value")
    ef.load_env_file()
    assert os.environ.get("COGNEE_TEST_FROM_FILE") == "file-value"
    assert os.environ.get("COGNEE_TEST_EXPORTED") == "shell-value"
    monkeypatch.delenv("COGNEE_TEST_FROM_FILE", raising=False)


def test_load_is_idempotent_per_process(env_file_env, monkeypatch):
    ef, fresh = env_file_env
    fresh("COGNEE_TEST_ONCE=first\n")
    ef.load_env_file()
    assert os.environ.get("COGNEE_TEST_ONCE") == "first"
    monkeypatch.delenv("COGNEE_TEST_ONCE")
    # Second call without a reset must not re-read the file.
    ef.load_env_file()
    assert "COGNEE_TEST_ONCE" not in os.environ


def test_denylist_never_injected(env_file_env, monkeypatch):
    ef, fresh = env_file_env
    original_path = os.environ.get("PATH", "")
    fresh(
        "PATH=/evil\nLD_PRELOAD=/evil.so\nDYLD_INSERT_LIBRARIES=/evil.dylib\n"
        "PYTHONPATH=/evil\nCOGNEE_TEST_OK=1\n"
    )
    monkeypatch.delenv("LD_PRELOAD", raising=False)
    monkeypatch.delenv("DYLD_INSERT_LIBRARIES", raising=False)
    ef.load_env_file()
    assert os.environ.get("PATH", "") == original_path
    assert "LD_PRELOAD" not in os.environ
    assert "DYLD_INSERT_LIBRARIES" not in os.environ
    assert os.environ.get("COGNEE_TEST_OK") == "1"
    monkeypatch.delenv("COGNEE_TEST_OK", raising=False)


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
    path = fresh("COGNEE_TEST_PERMS=1\n")
    path.chmod(0o644)
    ef.load_env_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    monkeypatch.delenv("COGNEE_TEST_PERMS", raising=False)


def test_env_file_path_override_and_default(env_file_env, temp_home, monkeypatch):
    ef, fresh = env_file_env
    override = fresh("A=1\n")
    assert ef.env_file_path() == override
    monkeypatch.delenv("COGNEE_ENV_FILE")
    # The default resolves under the per-test HOME, never the real one.
    assert ef.env_file_path() == temp_home / ".cognee" / ".env"


def test_template_not_written_when_override_set(env_file_env):
    ef, fresh = env_file_env
    fresh(None)
    assert ef.ensure_env_file_template() is False


def test_template_never_overwrites(env_file_env, temp_home):
    # No COGNEE_ENV_FILE override: the default location (inside the isolated
    # temp HOME, bound at the isolated import) gets the template exactly once.
    ef, _ = env_file_env
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


def test_status_reports_names_not_values(env_file_env, monkeypatch):
    ef, fresh = env_file_env
    fresh("COGNEE_TEST_SECRET=super-secret\nPATH=/evil\nCOGNEE_TEST_SHADOWED=file-value\n")
    monkeypatch.setenv("COGNEE_TEST_SHADOWED", "shell-value")
    ef.load_env_file()
    status = ef.env_file_status()
    assert status["exists"] is True
    assert "COGNEE_TEST_SECRET" in status["keys"]
    assert "super-secret" not in str(status)
    assert status["blocked"] == ["PATH"]
    assert status["overridden"] == ["COGNEE_TEST_SHADOWED"]
    monkeypatch.delenv("COGNEE_TEST_SECRET", raising=False)


def test_status_missing_file(env_file_env):
    ef, fresh = env_file_env
    path = fresh(None)
    status = ef.env_file_status()
    assert status == {"path": str(path), "exists": False}
