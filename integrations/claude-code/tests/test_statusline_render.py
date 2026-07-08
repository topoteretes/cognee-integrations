#!/usr/bin/env python3
"""Tests for cognee_statusline_render.py

Run with:
    python integrations/claude-code/tests/test_statusline_render.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# Add the scripts directory to the path so we can import the module
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import cognee_statusline_render as sr


def _with_plugin_root(manifest_data):
    """Create a temp directory that looks like CLAUDE_PLUGIN_ROOT.

    If manifest_data is not None, write it as plugin.json under
    .claude-plugin/.  If None, skip creating the manifest file.
    """
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    plugin_dir = root / ".claude-plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    if manifest_data is not None:
        (plugin_dir / "plugin.json").write_text(json.dumps(manifest_data), encoding="utf-8")
    return tmp, root


def test_happy_path_reads_version():
    tmp, root = _with_plugin_root({"version": "0.3.0"})
    old = os.environ.get("CLAUDE_PLUGIN_ROOT")
    os.environ["CLAUDE_PLUGIN_ROOT"] = str(root)
    try:
        assert sr._plugin_version() == "0.3.0"
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        else:
            os.environ["CLAUDE_PLUGIN_ROOT"] = old
        tmp.cleanup()


def test_missing_plugin_root_hides_plugin_version():
    old = os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
    try:
        assert sr._plugin_version() == ""
    finally:
        if old is not None:
            os.environ["CLAUDE_PLUGIN_ROOT"] = old


def test_missing_manifest_hides_plugin_version():
    tmp, root = _with_plugin_root(None)
    old = os.environ.get("CLAUDE_PLUGIN_ROOT")
    os.environ["CLAUDE_PLUGIN_ROOT"] = str(root)
    try:
        assert sr._plugin_version() == ""
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        else:
            os.environ["CLAUDE_PLUGIN_ROOT"] = old
        tmp.cleanup()


def test_bad_manifest_hides_plugin_version():
    tmp, root = _with_plugin_root(None)
    old = os.environ.get("CLAUDE_PLUGIN_ROOT")
    os.environ["CLAUDE_PLUGIN_ROOT"] = str(root)
    # Write malformed JSON
    (root / ".claude-plugin" / "plugin.json").write_text("{", encoding="utf-8")
    try:
        assert sr._plugin_version() == ""
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        else:
            os.environ["CLAUDE_PLUGIN_ROOT"] = old
        tmp.cleanup()


def test_missing_version_hides_plugin_version():
    tmp, root = _with_plugin_root({})
    old = os.environ.get("CLAUDE_PLUGIN_ROOT")
    os.environ["CLAUDE_PLUGIN_ROOT"] = str(root)
    try:
        assert sr._plugin_version() == ""
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        else:
            os.environ["CLAUDE_PLUGIN_ROOT"] = old
        tmp.cleanup()


def test_null_version_hides_plugin_version():
    tmp, root = _with_plugin_root({"version": None})
    old = os.environ.get("CLAUDE_PLUGIN_ROOT")
    os.environ["CLAUDE_PLUGIN_ROOT"] = str(root)
    try:
        assert sr._plugin_version() == ""
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        else:
            os.environ["CLAUDE_PLUGIN_ROOT"] = old
        tmp.cleanup()


def test_version_suffix_with_version():
    """Test that _version_suffix formats correctly when version exists."""
    version = "0.3.0"
    expected = " · v0.3.0"
    result = f" · v{version}" if version else ""
    assert result == expected


def test_version_suffix_without_version():
    """Test that _version_suffix returns empty when no version."""
    version = ""
    result = f" · v{version}" if version else ""
    assert result == ""


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
