"""Unit tests for _env_file (one-time config via ~/.cognee/.env).

Covers:
  - dotenv parsing (comments, quotes, `export ` prefix, malformed lines)
  - setdefault precedence: a real env var always beats the file
  - denylist enforcement (PATH & friends never injected)
  - missing / unreadable file is a silent no-op
  - permission tightening to 0600 on load
  - first-run template creation (and that it never overwrites)
  - env_file_status diagnostics (names only, override detection)

Every test runs under plain `python3 tests/test_env_file.py` (no pytest
fixtures) as well as under `pytest`, matching the sibling test convention.
"""

import os
import pathlib
import stat
import sys
import tempfile

_SCRIPTS_DIR = str(pathlib.Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import _env_file  # noqa: E402

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="cognee-env-file-test-"))


def _fresh(content: str | None, name: str = ".env") -> pathlib.Path:
    """Point COGNEE_ENV_FILE at a fresh temp file and reset the loader."""
    path = _TMP / name
    if path.exists():
        path.unlink()
    if content is not None:
        path.write_text(content, encoding="utf-8")
    os.environ["COGNEE_ENV_FILE"] = str(path)
    _env_file._loaded = False
    return path


def test_parse_basic_and_quotes():
    path = _fresh(
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
    parsed = _env_file.parse_env_file(path)
    assert parsed["PLAIN"] == "value"
    assert parsed["DQ"] == "double quoted"
    assert parsed["SQ"] == "single quoted"
    assert parsed["SPACED"] == "padded value"
    assert parsed["EXPORTED"] == "works"
    assert parsed["EMPTY"] == ""


def test_parse_skips_malformed_lines():
    path = _fresh(
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
    parsed = _env_file.parse_env_file(path)
    assert "no_equals_sign_here" not in parsed
    assert "BAD KEY" not in parsed
    assert "" not in parsed
    assert parsed["GOOD"] == "1"


def test_load_sets_missing_and_respects_existing():
    _fresh("COGNEE_TEST_FROM_FILE=file-value\nCOGNEE_TEST_EXPORTED=file-value\n")
    os.environ.pop("COGNEE_TEST_FROM_FILE", None)
    os.environ["COGNEE_TEST_EXPORTED"] = "shell-value"
    try:
        _env_file.load_env_file()
        assert os.environ.get("COGNEE_TEST_FROM_FILE") == "file-value"
        assert os.environ.get("COGNEE_TEST_EXPORTED") == "shell-value"
    finally:
        os.environ.pop("COGNEE_TEST_FROM_FILE", None)
        os.environ.pop("COGNEE_TEST_EXPORTED", None)


def test_load_is_idempotent_per_process():
    _fresh("COGNEE_TEST_ONCE=first\n")
    os.environ.pop("COGNEE_TEST_ONCE", None)
    try:
        _env_file.load_env_file()
        assert os.environ.get("COGNEE_TEST_ONCE") == "first"
        os.environ.pop("COGNEE_TEST_ONCE", None)
        # Second call without a reset must not re-read the file.
        _env_file.load_env_file()
        assert "COGNEE_TEST_ONCE" not in os.environ
    finally:
        os.environ.pop("COGNEE_TEST_ONCE", None)


def test_denylist_never_injected():
    original_path = os.environ.get("PATH", "")
    _fresh(
        "PATH=/evil\nLD_PRELOAD=/evil.so\nDYLD_INSERT_LIBRARIES=/evil.dylib\nPYTHONPATH=/evil\nCOGNEE_TEST_OK=1\n"
    )
    os.environ.pop("LD_PRELOAD", None)
    os.environ.pop("DYLD_INSERT_LIBRARIES", None)
    os.environ.pop("COGNEE_TEST_OK", None)
    try:
        _env_file.load_env_file()
        assert os.environ.get("PATH", "") == original_path
        assert "LD_PRELOAD" not in os.environ
        assert "DYLD_INSERT_LIBRARIES" not in os.environ
        assert os.environ.get("COGNEE_TEST_OK") == "1"
    finally:
        os.environ.pop("COGNEE_TEST_OK", None)


def test_parse_tolerates_utf8_bom_and_crlf():
    # Windows PowerShell 5.1 writes UTF-8 with a BOM and CRLF line endings;
    # the BOM must not glue onto the first key name.
    path = _fresh(None)
    path.write_bytes(b'\xef\xbb\xbfCOGNEE_TEST_BOM="first"\r\nCOGNEE_TEST_SECOND=2\r\n')
    parsed = _env_file.parse_env_file(path)
    assert parsed["COGNEE_TEST_BOM"] == "first"
    assert parsed["COGNEE_TEST_SECOND"] == "2"


def test_parse_tolerates_utf16():
    # A PowerShell 5.1 `>` redirect (Out-File) writes UTF-16 LE with a BOM.
    path = _fresh(None)
    path.write_text('COGNEE_TEST_U16="wide"\n', encoding="utf-16")
    parsed = _env_file.parse_env_file(path)
    assert parsed["COGNEE_TEST_U16"] == "wide"


def test_missing_file_is_noop():
    _fresh(None)
    _env_file.load_env_file()  # must not raise


def test_load_tightens_permissions():
    if os.name == "nt":
        return
    path = _fresh("COGNEE_TEST_PERMS=1\n")
    path.chmod(0o644)
    try:
        _env_file.load_env_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        os.environ.pop("COGNEE_TEST_PERMS", None)


def test_env_file_path_override_and_default():
    override = _fresh("A=1\n")
    assert _env_file.env_file_path() == override
    os.environ.pop("COGNEE_ENV_FILE", None)
    assert _env_file.env_file_path() == pathlib.Path.home() / ".cognee" / ".env"


def test_template_not_written_when_override_set():
    _fresh(None)
    assert _env_file.ensure_env_file_template() is False


def test_template_never_overwrites():
    # Redirect the default location into the temp dir for this test.
    os.environ.pop("COGNEE_ENV_FILE", None)
    orig_home, orig_default = _env_file._COGNEE_HOME, _env_file._DEFAULT_ENV_FILE
    _env_file._COGNEE_HOME = _TMP / "cognee-home"
    _env_file._DEFAULT_ENV_FILE = _env_file._COGNEE_HOME / ".env"
    try:
        assert _env_file.ensure_env_file_template() is True
        text = _env_file._DEFAULT_ENV_FILE.read_text(encoding="utf-8")
        assert "COGNEE_BASE_URL" in text
        assert all(line.startswith("#") or not line for line in text.splitlines())
        if os.name != "nt":
            assert stat.S_IMODE(_env_file._DEFAULT_ENV_FILE.stat().st_mode) == 0o600
        _env_file._DEFAULT_ENV_FILE.write_text("USER_EDIT=kept\n", encoding="utf-8")
        assert _env_file.ensure_env_file_template() is False
        assert _env_file._DEFAULT_ENV_FILE.read_text(encoding="utf-8") == "USER_EDIT=kept\n"
    finally:
        _env_file._COGNEE_HOME = orig_home
        _env_file._DEFAULT_ENV_FILE = orig_default


def test_status_reports_names_not_values():
    _fresh("COGNEE_TEST_SECRET=super-secret\nPATH=/evil\nCOGNEE_TEST_SHADOWED=file-value\n")
    os.environ["COGNEE_TEST_SHADOWED"] = "shell-value"
    os.environ.pop("COGNEE_TEST_SECRET", None)
    try:
        _env_file.load_env_file()
        status = _env_file.env_file_status()
        assert status["exists"] is True
        assert "COGNEE_TEST_SECRET" in status["keys"]
        assert "super-secret" not in str(status)
        assert status["blocked"] == ["PATH"]
        assert status["overridden"] == ["COGNEE_TEST_SHADOWED"]
    finally:
        os.environ.pop("COGNEE_TEST_SECRET", None)
        os.environ.pop("COGNEE_TEST_SHADOWED", None)


def test_status_missing_file():
    path = _fresh(None)
    status = _env_file.env_file_status()
    assert status == {"path": str(path), "exists": False}


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if not _name.startswith("test_") or not callable(_fn):
            continue
        try:
            _fn()
            print(f"PASS {_name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {_name}: {e}")
    sys.exit(1 if failures else 0)
