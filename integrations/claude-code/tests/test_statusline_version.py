"""Tests for the plugin-version segment in the status line.

Verifies that ``_plugin_version()`` reads the ``version`` field from
``CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json`` and that the status
line renders it as ``… · v<installed>``.

Missing/unreadable manifests must not crash; they just omit the version.

Run: python integrations/claude-code/tests/test_statusline_version.py
"""

import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import cognee_statusline_render as slr  # noqa: E402


def test_reads_version_from_manifest():
    """Happy path: valid plugin.json → 'v<version>'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manifest_dir = pathlib.Path(tmpdir) / ".claude-plugin"
        manifest_dir.mkdir()
        manifest = manifest_dir / "plugin.json"
        manifest.write_text(json.dumps({"version": "0.3.0"}), encoding="utf-8")

        old = os.environ.get("CLAUDE_PLUGIN_ROOT")
        os.environ["CLAUDE_PLUGIN_ROOT"] = tmpdir
        try:
            assert slr._plugin_version() == "v0.3.0"
        finally:
            if old is None:
                os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
            else:
                os.environ["CLAUDE_PLUGIN_ROOT"] = old


def test_missing_env_var_returns_empty():
    """No CLAUDE_PLUGIN_ROOT → empty string, no crash."""
    old = os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
    try:
        assert slr._plugin_version() == ""
    finally:
        if old is not None:
            os.environ["CLAUDE_PLUGIN_ROOT"] = old


def test_missing_manifest_returns_empty():
    """CLAUDE_PLUGIN_ROOT set but no plugin.json → empty string, no crash."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old = os.environ.get("CLAUDE_PLUGIN_ROOT")
        os.environ["CLAUDE_PLUGIN_ROOT"] = tmpdir
        try:
            assert slr._plugin_version() == ""
        finally:
            if old is None:
                os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
            else:
                os.environ["CLAUDE_PLUGIN_ROOT"] = old


def test_invalid_json_returns_empty():
    """Corrupt plugin.json → empty string, no crash."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manifest_dir = pathlib.Path(tmpdir) / ".claude-plugin"
        manifest_dir.mkdir()
        manifest = manifest_dir / "plugin.json"
        manifest.write_text("{not valid json", encoding="utf-8")

        old = os.environ.get("CLAUDE_PLUGIN_ROOT")
        os.environ["CLAUDE_PLUGIN_ROOT"] = tmpdir
        try:
            assert slr._plugin_version() == ""
        finally:
            if old is None:
                os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
            else:
                os.environ["CLAUDE_PLUGIN_ROOT"] = old


def test_missing_version_key_returns_empty():
    """plugin.json without 'version' key → empty string."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manifest_dir = pathlib.Path(tmpdir) / ".claude-plugin"
        manifest_dir.mkdir()
        manifest = manifest_dir / "plugin.json"
        manifest.write_text(json.dumps({"name": "test"}), encoding="utf-8")

        old = os.environ.get("CLAUDE_PLUGIN_ROOT")
        os.environ["CLAUDE_PLUGIN_ROOT"] = tmpdir
        try:
            assert slr._plugin_version() == ""
        finally:
            if old is None:
                os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
            else:
                os.environ["CLAUDE_PLUGIN_ROOT"] = old


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
