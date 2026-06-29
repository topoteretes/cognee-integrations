"""Unit tests for the pure-local Cognee statusline renderer.

Run: python integrations/claude-code/tests/test_statusline_render.py
(or via pytest).
"""

import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import cognee_statusline_render as sr  # noqa: E402


def _with_plugin_root(manifest):
    tmp = tempfile.TemporaryDirectory(prefix="cognee-statusline-test-")
    root = pathlib.Path(tmp.name)
    plugin_dir = root / ".claude-plugin"
    plugin_dir.mkdir()
    if manifest is not None:
        (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp, root


def test_reads_plugin_version_from_claude_plugin_root():
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
