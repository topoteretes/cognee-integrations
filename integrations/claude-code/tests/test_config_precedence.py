"""Config precedence tests for the Cognee claude-code plugin (#3559).

Documents the ACTUAL current behavior: load_config() merges defaults, then the
config file, then environment variables -- so **env wins** over the config file
here (see scripts/config.py). This file only pins that behavior down; it changes
no runtime logic.

Run: python integrations/claude-code/tests/test_config_precedence.py
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402
import config  # noqa: E402


def _snapshot_env(*keys):
    return {k: os.environ.get(k) for k in keys}


def _restore_env(saved):
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _write_config_file(directory, payload):
    """Point config._CONFIG_FILE at a temp file holding `payload` (JSON)."""
    path = pathlib.Path(directory) / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_env_overrides_file():
    orig_file = config._CONFIG_FILE
    saved = _snapshot_env("COGNEE_PLUGIN_DATASET")
    tmpdir = tempfile.mkdtemp()
    try:
        config._CONFIG_FILE = _write_config_file(tmpdir, {"dataset": "from_file"})
        os.environ["COGNEE_PLUGIN_DATASET"] = "from_env"
        # Env is the last layer, so it wins over the config file.
        assert config.load_config()["dataset"] == "from_env"
    finally:
        config._CONFIG_FILE = orig_file
        _restore_env(saved)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_file_over_default():
    orig_file = config._CONFIG_FILE
    saved = _snapshot_env("COGNEE_PLUGIN_DATASET")
    tmpdir = tempfile.mkdtemp()
    try:
        config._CONFIG_FILE = _write_config_file(tmpdir, {"dataset": "from_file"})
        os.environ.pop("COGNEE_PLUGIN_DATASET", None)
        assert config.load_config()["dataset"] == "from_file"
    finally:
        config._CONFIG_FILE = orig_file
        _restore_env(saved)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_default_alone():
    orig_file = config._CONFIG_FILE
    saved = _snapshot_env("COGNEE_PLUGIN_DATASET")
    tmpdir = tempfile.mkdtemp()
    try:
        # Non-existent path -> the file layer is skipped entirely.
        config._CONFIG_FILE = pathlib.Path(tmpdir) / "config.json"
        os.environ.pop("COGNEE_PLUGIN_DATASET", None)
        assert config.load_config()["dataset"] == "agent_sessions"
    finally:
        config._CONFIG_FILE = orig_file
        _restore_env(saved)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_footgun1_empty_ignored():
    orig_file = config._CONFIG_FILE
    saved = _snapshot_env("COGNEE_PLUGIN_DATASET")
    tmpdir = tempfile.mkdtemp()
    try:
        # File layer: "" is filtered out (`v != ""`), so it cannot override.
        config._CONFIG_FILE = _write_config_file(tmpdir, {"dataset": ""})
        os.environ.pop("COGNEE_PLUGIN_DATASET", None)
        assert config.load_config()["dataset"] == "agent_sessions"

        # Env layer: "" is skipped (`if val:`), so it cannot override the file.
        config._CONFIG_FILE = _write_config_file(tmpdir, {"dataset": "from_file"})
        os.environ["COGNEE_PLUGIN_DATASET"] = ""
        assert config.load_config()["dataset"] == "from_file"
    finally:
        config._CONFIG_FILE = orig_file
        _restore_env(saved)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_backend_clearing_native():
    orig_file = config._CONFIG_FILE
    saved = _snapshot_env("COGNEE_CLAUDE_BACKEND", "COGNEE_BASE_URL", "COGNEE_API_KEY")
    tmpdir = tempfile.mkdtemp()
    try:
        config._CONFIG_FILE = _write_config_file(
            tmpdir, {"base_url": "http://example", "api_key": "secret"}
        )
        os.environ.pop("COGNEE_BASE_URL", None)
        os.environ.pop("COGNEE_API_KEY", None)
        os.environ["COGNEE_CLAUDE_BACKEND"] = "native"

        merged = config.load_config()
        # native backend forces local mode: base_url and api_key are cleared.
        assert merged["base_url"] == ""
        assert merged["api_key"] == ""
    finally:
        config._CONFIG_FILE = orig_file
        _restore_env(saved)
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "#3559: COGNEE_COGNIFY_POLL_INTERVAL is registered in _ENV_MAP for "
        "config-file support, but consumers read it via _float_env() straight "
        "from os.environ, so the config-file value never reaches runtime (dead "
        "knob)."
    ),
)
def test_footgun3_config_file_poll_interval_reaches_consumer():
    # The poll interval is set ONLY in the config file, with the env var unset,
    # to isolate the config-file path. load_config() surfaces it, but the real
    # consumer reads it through _float_env(name, default) -- which ignores the
    # merged config -- so the config-file value is silently dropped.
    orig_file = config._CONFIG_FILE
    saved = _snapshot_env("COGNEE_COGNIFY_POLL_INTERVAL")
    tmpdir = tempfile.mkdtemp()
    try:
        config._CONFIG_FILE = _write_config_file(tmpdir, {"cognify_poll_interval": 99.0})
        os.environ.pop("COGNEE_COGNIFY_POLL_INTERVAL", None)

        # Sanity: load_config() DOES carry the config-file value through.
        assert config.load_config()["cognify_poll_interval"] == 99.0

        # Desired behavior: the value the consumer sees (via _float_env, exactly
        # as scripts/_plugin_common.py:1401 calls it) should be the config-file
        # value. Under current code _float_env returns the hardcoded 3.0 default
        # because it reads os.environ (empty), so this assertion fails today.
        consumer_value = pc._float_env("COGNEE_COGNIFY_POLL_INTERVAL", 3.0)
        assert consumer_value == 99.0
    finally:
        config._CONFIG_FILE = orig_file
        _restore_env(saved)
        shutil.rmtree(tmpdir, ignore_errors=True)
