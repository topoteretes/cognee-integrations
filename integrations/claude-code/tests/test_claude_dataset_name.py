"""Dataset-name sanitization tests for the Claude Code integration."""

import importlib.util
import os
import pathlib
import sys

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load_config_module():
    spec = importlib.util.spec_from_file_location(
        "claude_code_dataset_config", SCRIPTS / "config.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cfg = _load_config_module()


def test_sanitize_dataset_name_matches_session_rule():
    assert cfg.sanitize_dataset_name("valid-Name_1.2") == "valid-Name_1.2"
    assert cfg.sanitize_dataset_name(" project/name!* ") == "project_name"
    assert cfg.sanitize_dataset_name("..__project__..") == "project"
    assert cfg.sanitize_dataset_name("a" * 130) == "a" * 120
    assert cfg.sanitize_dataset_name(" !!! ") == "agent_sessions"


def test_get_dataset_normalizes_config_value():
    assert cfg.get_dataset({"dataset": " ../bad dataset!* "}) == "bad_dataset"
    assert cfg.get_dataset({"dataset": "..."}) == "agent_sessions"


def test_load_config_normalizes_env_dataset():
    old_env = os.environ.get("COGNEE_PLUGIN_DATASET")
    try:
        os.environ["COGNEE_PLUGIN_DATASET"] = " env/dataset! "
        assert cfg.load_config()["dataset"] == "env_dataset"
    finally:
        if old_env is None:
            os.environ.pop("COGNEE_PLUGIN_DATASET", None)
        else:
            os.environ["COGNEE_PLUGIN_DATASET"] = old_env


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", name, exc)
    sys.exit(1 if failures else 0)
