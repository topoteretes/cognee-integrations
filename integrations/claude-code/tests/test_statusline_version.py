"""Tests for the status-line plugin-version read (`_plugin_version`).

The status line appends `· v<version>` read from the installed plugin manifest
at `$CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json`. The read must be pure-local
and fail-silent: any missing env var or unreadable/malformed manifest yields ""
(no version shown) rather than raising.

Run: python integrations/claude-code/tests/test_statusline_version.py
"""

import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import cognee_statusline_render as sr  # noqa: E402


def _write_manifest(root, contents):
    """Create <root>/.claude-plugin/plugin.json holding `contents` (raw text)."""
    manifest_dir = pathlib.Path(root) / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(contents, encoding="utf-8")


def test_reads_version_from_manifest():
    with tempfile.TemporaryDirectory() as root:
        _write_manifest(root, json.dumps({"name": "cognee-memory", "version": "0.2.0"}))
        os.environ["CLAUDE_PLUGIN_ROOT"] = root
        try:
            assert sr._plugin_version() == "0.2.0"
        finally:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)


def test_no_env_var_yields_empty():
    os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
    assert sr._plugin_version() == ""


def test_missing_manifest_yields_empty():
    with tempfile.TemporaryDirectory() as root:
        # root exists but no .claude-plugin/plugin.json inside it
        os.environ["CLAUDE_PLUGIN_ROOT"] = root
        try:
            assert sr._plugin_version() == ""
        finally:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)


def test_malformed_manifest_yields_empty():
    with tempfile.TemporaryDirectory() as root:
        _write_manifest(root, "{ not valid json ")
        os.environ["CLAUDE_PLUGIN_ROOT"] = root
        try:
            assert sr._plugin_version() == ""
        finally:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)


def test_manifest_without_version_key_yields_empty():
    with tempfile.TemporaryDirectory() as root:
        _write_manifest(root, json.dumps({"name": "cognee-memory"}))
        os.environ["CLAUDE_PLUGIN_ROOT"] = root
        try:
            assert sr._plugin_version() == ""
        finally:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)


def test_full_line_includes_version_suffix():
    """End-to-end: main() appends the suffix when a manifest is present."""
    import io
    from contextlib import redirect_stdout

    with tempfile.TemporaryDirectory() as root:
        _write_manifest(root, json.dumps({"version": "9.9.9"}))
        os.environ["CLAUDE_PLUGIN_ROOT"] = root
        # Force a deterministic local line (no env-driven dataset/mode).
        os.environ.pop("COGNEE_PLUGIN_DATASET", None)
        os.environ.pop("COGNEE_BASE_URL", None)
        stdin_backup = sys.stdin
        sys.stdin = io.StringIO("{}")
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                sr.main()
            assert out.getvalue().endswith("· v9.9.9")
        finally:
            sys.stdin = stdin_backup
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
    sys.exit(1 if failures else 0)
