import json
import sys
import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

# We need to insert paths to import from both plugins
_ROOT = Path(__file__).resolve().parent.parent.parent.parent

_CLAUDE_CODE_SCRIPTS = _ROOT / "integrations" / "claude-code" / "scripts"
_CODEX_SCRIPTS = _ROOT / "integrations" / "codex" / "plugins" / "cognee" / "scripts"

sys.path.insert(0, str(_CLAUDE_CODE_SCRIPTS))
import cognee_statusline_render as claude_render
sys.path.pop(0)

sys.path.insert(0, str(_CODEX_SCRIPTS))
import cognee_statusline_render as codex_render
sys.path.pop(0)


class TestStatuslineVersion(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.tmp_path = Path(self.temp_dir)
        self.claude_dir = self.tmp_path / ".claude-plugin"
        self.codex_dir = self.tmp_path / ".codex-plugin"
        self.claude_dir.mkdir(parents=True, exist_ok=True)
        self.codex_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_statusline_version_parity(self):
        """
        Test that the fail-silent version logic and update badges format
        identically in both the Claude Code plugin and Codex plugin.
        """
        
        # 1. No files exist - should return empty string
        with patch("pathlib.Path.home", return_value=self.tmp_path):
            claude_val = claude_render._plugin_version()
            codex_val = codex_render._plugin_version()
            self.assertEqual(claude_val, "")
            self.assertEqual(codex_val, "")
            self.assertEqual(claude_val, codex_val)

        # 2. Only plugin.json exists
        plugin_data = {"version": "1.0.0"}
        (self.claude_dir / "plugin.json").write_text(json.dumps(plugin_data), encoding="utf-8")
        (self.codex_dir / "plugin.json").write_text(json.dumps(plugin_data), encoding="utf-8")

        with patch("pathlib.Path.home", return_value=self.tmp_path):
            claude_val = claude_render._plugin_version()
            codex_val = codex_render._plugin_version()
            self.assertEqual(claude_val, " v1.0.0")
            self.assertEqual(claude_val, codex_val)

        # 3. Both exist, but no update available
        update_data_no_update = {"latest_version": "1.0.0"}
        (self.claude_dir / "update-check.json").write_text(json.dumps(update_data_no_update), encoding="utf-8")
        (self.codex_dir / "update-check.json").write_text(json.dumps(update_data_no_update), encoding="utf-8")

        with patch("pathlib.Path.home", return_value=self.tmp_path):
            claude_val = claude_render._plugin_version()
            codex_val = codex_render._plugin_version()
            self.assertEqual(claude_val, " v1.0.0")
            self.assertEqual(claude_val, codex_val)

        # 4. Both exist, update available
        update_data_update = {"latest_version": "1.1.0"}
        (self.claude_dir / "update-check.json").write_text(json.dumps(update_data_update), encoding="utf-8")
        (self.codex_dir / "update-check.json").write_text(json.dumps(update_data_update), encoding="utf-8")

        with patch("pathlib.Path.home", return_value=self.tmp_path):
            claude_val = claude_render._plugin_version()
            codex_val = codex_render._plugin_version()
            self.assertEqual(claude_val, " v1.0.0 ↑ v1.1.0")
            self.assertEqual(claude_val, codex_val)

        # 5. Invalid JSON should be fail-silent and just return what's available
        (self.claude_dir / "update-check.json").write_text("invalid json", encoding="utf-8")
        (self.codex_dir / "update-check.json").write_text("invalid json", encoding="utf-8")

        with patch("pathlib.Path.home", return_value=self.tmp_path):
            claude_val = claude_render._plugin_version()
            codex_val = codex_render._plugin_version()
            self.assertEqual(claude_val, " v1.0.0")
            self.assertEqual(claude_val, codex_val)

if __name__ == "__main__":
    unittest.main()
