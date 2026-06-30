import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import cognee_statusline_render as sl  # noqa: E402


def test_installed_version_reads_manifest(monkeypatch, tmp_path):
    root = tmp_path / "plugin-root"
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"version": "0.3.0"}), encoding="utf-8")

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.setattr(sl, "_PLUGIN_MANIFEST_PATH", manifest)

    assert sl._installed_version() == "0.3.0"
    assert sl._version_suffix() == " · v0.3.0"


def test_installed_version_is_fail_silent(monkeypatch, tmp_path):
    missing = tmp_path / "missing" / ".claude-plugin" / "plugin.json"
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "missing"))
    monkeypatch.setattr(sl, "_PLUGIN_MANIFEST_PATH", missing)

    assert sl._installed_version() == ""
    assert sl._version_suffix() == ""
