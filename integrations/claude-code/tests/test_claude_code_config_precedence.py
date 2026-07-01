import importlib.util
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "scripts" / "config.py"


def load_module(tmp_path):
    spec = importlib.util.spec_from_file_location("claude_code_config_under_test", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._CONFIG_FILE = tmp_path / "config.json"
    module._CONFIG_DIR = tmp_path
    module._STATE_DIR = tmp_path
    module._HOOK_LOG = tmp_path / "hook.log"
    return module


def clear_config_env(monkeypatch, module):
    for env_key in module._ENV_MAP:
        monkeypatch.delenv(env_key, raising=False)


def test_defaults_when_no_env_or_config(tmp_path, monkeypatch):
    module = load_module(tmp_path)
    clear_config_env(monkeypatch, module)

    assert module.load_config() == module._DEFAULTS


def test_config_file_overrides_defaults(tmp_path, monkeypatch):
    module = load_module(tmp_path)
    clear_config_env(monkeypatch, module)
    file_values = {
        key: f"file-{key}" if not isinstance(default, (int, float, bool)) else default + 1
        for key, default in module._DEFAULTS.items()
    }
    file_values["prefer_cognee_memory"] = False
    module._CONFIG_FILE.write_text(json.dumps(file_values), encoding="utf-8")

    cfg = module.load_config()

    for key, expected in file_values.items():
        assert cfg[key] == expected


def test_env_overrides_config_file_for_every_mapped_setting(tmp_path, monkeypatch):
    module = load_module(tmp_path)
    clear_config_env(monkeypatch, module)
    module._CONFIG_FILE.write_text(
        json.dumps({config_key: f"file-{config_key}" for config_key in module._ENV_MAP.values()}),
        encoding="utf-8",
    )

    for env_key, config_key in module._ENV_MAP.items():
        monkeypatch.setenv(env_key, f"env-{config_key}")

    cfg = module.load_config()

    for _, config_key in module._ENV_MAP.items():
        assert cfg[config_key].startswith("env-")


def test_empty_env_and_empty_file_values_do_not_override(tmp_path, monkeypatch):
    module = load_module(tmp_path)
    clear_config_env(monkeypatch, module)
    module._CONFIG_FILE.write_text(json.dumps({"dataset": "", "agent_name": None}), encoding="utf-8")
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "")

    cfg = module.load_config()

    assert cfg["dataset"] == module._DEFAULTS["dataset"]
    assert cfg["agent_name"] == module._DEFAULTS["agent_name"]
