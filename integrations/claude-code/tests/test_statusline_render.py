"""Tests for cognee_statusline_render.py."""

import json
import pathlib
import sys

# Make scripts importable
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import cognee_statusline_render as sr


def test_reads_plugin_version(tmp_path):
    manifest = tmp_path / "plugin.json"
    manifest.write_text(
        json.dumps({"version": "0.2.0"}),
        encoding="utf-8",
    )

    original = sr._PLUGIN_MANIFEST_PATH
    sr._PLUGIN_MANIFEST_PATH = manifest

    try:
        assert sr._plugin_version() == "0.2.0"
    finally:
        sr._PLUGIN_MANIFEST_PATH = original


def test_missing_manifest_returns_empty(tmp_path):
    original = sr._PLUGIN_MANIFEST_PATH
    sr._PLUGIN_MANIFEST_PATH = tmp_path / "missing.json"

    try:
        assert sr._plugin_version() == ""
    finally:
        sr._PLUGIN_MANIFEST_PATH = original


def test_invalid_manifest_returns_empty(tmp_path):
    manifest = tmp_path / "plugin.json"
    manifest.write_text("{invalid json}", encoding="utf-8")

    original = sr._PLUGIN_MANIFEST_PATH
    sr._PLUGIN_MANIFEST_PATH = manifest

    try:
        assert sr._plugin_version() == ""
    finally:
        sr._PLUGIN_MANIFEST_PATH = original