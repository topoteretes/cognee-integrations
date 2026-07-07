import json
import os
import sys
from pathlib import Path

import pytest

# Add scripts directory to path to allow importing config
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import config


@pytest.fixture(autouse=True)
def setup_teardown(monkeypatch, tmp_path):
    # Mock home directory to isolate global config
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    # Reload DEFAULTS to reset state just in case
    config._CONFIG_DIR = home / ".cognee-plugin" / "claude-code"
    config._CONFIG_FILE = config._CONFIG_DIR / "config.json"

    # Ensure no relevant env vars are set
    for env_var in list(os.environ.keys()):
        if env_var.startswith("COGNEE_") or env_var == "CLAUDE_CWD":
            monkeypatch.delenv(env_var, raising=False)


def write_picker(cwd_path: Path, data):
    cognee_dir = cwd_path / ".cognee"
    cognee_dir.mkdir(parents=True, exist_ok=True)
    picker_file = cognee_dir / "session-config.json"
    if isinstance(data, str):
        picker_file.write_text(data, encoding="utf-8")
    else:
        picker_file.write_text(json.dumps(data), encoding="utf-8")


def write_global_config(data):
    config._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config._CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")


def test_picker_resolves_via_explicit_cwd_arg(tmp_path):
    write_picker(tmp_path, {"dataset": "picker-dataset"})
    cfg = config.load_config(cwd=str(tmp_path))
    assert cfg.get("dataset") == "picker-dataset"


def test_picker_resolves_via_claude_cwd_env_fallback(monkeypatch, tmp_path):
    write_picker(tmp_path, {"dataset": "picker-env-dataset"})
    monkeypatch.setenv("CLAUDE_CWD", str(tmp_path))

    # mock os.getcwd to somewhere else to ensure CLAUDE_CWD is preferred
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)

    cfg = config.load_config()
    assert cfg.get("dataset") == "picker-env-dataset"


def test_picker_falls_back_to_os_getcwd_last(monkeypatch, tmp_path):
    write_picker(tmp_path, {"dataset": "picker-cwd-dataset"})
    monkeypatch.chdir(tmp_path)

    cfg = config.load_config()
    assert cfg.get("dataset") == "picker-cwd-dataset"


def test_precedence_env_beats_picker(tmp_path, monkeypatch):
    write_picker(tmp_path, {"dataset": "picker-dataset"})
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "env-dataset")

    cfg = config.load_config(cwd=str(tmp_path))
    assert cfg.get("dataset") == "env-dataset"


def test_precedence_picker_beats_global_config(tmp_path):
    write_global_config({"dataset": "global-dataset"})
    write_picker(tmp_path, {"dataset": "picker-dataset"})

    cfg = config.load_config(cwd=str(tmp_path))
    assert cfg.get("dataset") == "picker-dataset"


def test_precedence_config_beats_default(tmp_path):
    write_global_config({"dataset": "global-dataset"})

    cfg = config.load_config(cwd=str(tmp_path))
    assert cfg.get("dataset") == "global-dataset"


def test_missing_picker_file_falls_through_cleanly(tmp_path):
    # No .cognee dir at all
    cfg = config.load_config(cwd=str(tmp_path))
    assert cfg.get("dataset") == "agent_sessions"


def test_malformed_json_picker_falls_through(tmp_path):
    write_picker(tmp_path, "{invalid_json:")

    cfg = config.load_config(cwd=str(tmp_path))
    assert cfg.get("dataset") == "agent_sessions"


def test_null_dataset_value_falls_through(tmp_path):
    write_global_config({"dataset": "global-dataset"})
    write_picker(tmp_path, {"dataset": None})

    cfg = config.load_config(cwd=str(tmp_path))
    assert cfg.get("dataset") == "global-dataset"


def test_empty_string_dataset_falls_through(tmp_path):
    write_global_config({"dataset": "global-dataset"})
    write_picker(tmp_path, {"dataset": ""})

    cfg = config.load_config(cwd=str(tmp_path))
    assert cfg.get("dataset") == "global-dataset"


def test_picker_file_is_not_a_dict(tmp_path):
    write_global_config({"dataset": "global-dataset"})
    write_picker(tmp_path, ["list", "instead", "of", "dict"])

    cfg = config.load_config(cwd=str(tmp_path))
    assert cfg.get("dataset") == "global-dataset"


def test_picker_ignores_sensitive_keys(tmp_path):
    # A repo-committed picker file must not be able to redirect the backend or
    # inject credentials — only dataset/session selection is honored.
    write_picker(
        tmp_path,
        {
            "dataset": "picker-dataset",
            "base_url": "http://evil.example",
            "api_key": "ck_stolen",
            "llm_api_key": "sk-stolen",
            "backend": "server",
        },
    )
    cfg = config.load_config(cwd=str(tmp_path))
    assert cfg.get("dataset") == "picker-dataset"  # allowlisted key applied
    assert cfg.get("base_url") == ""  # sensitive keys ignored (default)
    assert cfg.get("api_key") == ""
    assert cfg.get("llm_api_key") == ""


def test_picker_honors_allowlisted_nondataset_key(tmp_path):
    write_picker(tmp_path, {"session_strategy": "git-branch"})
    cfg = config.load_config(cwd=str(tmp_path))
    assert cfg.get("session_strategy") == "git-branch"


def test_picker_unknown_key_ignored(tmp_path):
    write_picker(tmp_path, {"dataset": "picker-dataset", "totally_unknown": "x"})
    cfg = config.load_config(cwd=str(tmp_path))
    assert cfg.get("dataset") == "picker-dataset"
    assert "totally_unknown" not in cfg
