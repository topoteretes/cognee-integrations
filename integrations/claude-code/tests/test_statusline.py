"""Unit tests for the status-line version helper (_plugin_version)."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

# Import the module under test
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cognee_statusline_render as m


def _write_manifest(root: Path, version: str) -> None:
    plugin_dir = root / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"name": "cognee-memory", "version": version}), encoding="utf-8"
    )


def test_plugin_version_missing_env() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert m._plugin_version() == ""


def test_plugin_version_empty_env() -> None:
    with patch.dict(os.environ, {"CLAUDE_PLUGIN_ROOT": ""}):
        assert m._plugin_version() == ""


def test_plugin_version_no_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        manifest = Path(tmp) / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        # No file written — should fail silent
        assert manifest.parent.exists()
        with patch.dict(os.environ, {"CLAUDE_PLUGIN_ROOT": str(tmp)}):
            assert m._plugin_version() == ""


def test_plugin_version_valid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _write_manifest(Path(tmp), "0.3.0")
        with patch.dict(os.environ, {"CLAUDE_PLUGIN_ROOT": str(tmp)}):
            assert m._plugin_version() == " · v0.3.0"


def test_plugin_version_invalid_json() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = Path(tmp) / ".claude-plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text("not json", encoding="utf-8")
        with patch.dict(os.environ, {"CLAUDE_PLUGIN_ROOT": str(tmp)}):
            assert m._plugin_version() == ""


def test_plugin_version_missing_version_field() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = Path(tmp) / ".claude-plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(
            json.dumps({"name": "cognee-memory"}), encoding="utf-8"
        )
        with patch.dict(os.environ, {"CLAUDE_PLUGIN_ROOT": str(tmp)}):
            assert m._plugin_version() == ""
