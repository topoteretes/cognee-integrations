"""Config precedence matrix: env > config file > defaults (config.py).

Run: python integrations/codex/plugins/cognee/tests/test_config_precedence.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def _load_config_module():
    spec = importlib.util.spec_from_file_location("codex_config", _SCRIPTS / "config.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


PRECEDENCE_MATRIX = [
    ("dataset", "COGNEE_PLUGIN_DATASET", "file_dataset", "env_dataset", "agent_sessions", {}, {}),
    ("agent_name", "COGNEE_AGENT_NAME", "file_agent", "env_agent", "codex-agent", {}, {}),
    (
        "session_strategy",
        "COGNEE_SESSION_STRATEGY",
        "git-branch",
        "static",
        "per-directory",
        {},
        {},
    ),
    ("session_prefix", "COGNEE_SESSION_PREFIX", "file_prefix", "env_prefix", "codex", {}, {}),
    (
        "base_url",
        "COGNEE_BASE_URL",
        "http://file:9000",
        "http://env:8000",
        "",
        {"COGNEE_CODEX_BACKEND": "http"},
        {},
    ),
    (
        "api_key",
        "COGNEE_API_KEY",
        "file-key",
        "env-key",
        "",
        {"COGNEE_CODEX_BACKEND": "http", "COGNEE_BASE_URL": "http://env:8000"},
        {"base_url": "http://file:9000", "backend": "http"},
    ),
    ("user_email", "COGNEE_USER_EMAIL", "file@example.com", "env@example.com", "default_user@example.com", {}, {}),
    (
        "user_password",
        "COGNEE_USER_PASSWORD",
        "file-pass",
        "env-pass",
        "default_password",
        {},
        {},
    ),
    ("llm_api_key", "LLM_API_KEY", "file-llm", "env-llm", "", {}, {}),
    ("llm_model", "LLM_MODEL", "file-model", "env-model", "", {}, {}),
    (
        "_static_session_id",
        "COGNEE_SESSION_ID",
        "file-session",
        "env-session",
        None,
        {},
        {},
    ),
    ("backend", "COGNEE_CODEX_BACKEND", "http", "cloud", "auto", {}, {}),
]


def _coerce(actual, expected):
    if expected is None:
        return actual is None or actual == ""
    return actual == expected


def test_precedence_matrix():
    mod = _load_config_module()
    config_file = pathlib.Path(tempfile.mktemp(suffix=".json"))
    mod._CONFIG_FILE = config_file

    all_env_keys = {row[1] for row in PRECEDENCE_MATRIX}
    all_env_keys.update(key for row in PRECEDENCE_MATRIX for key in row[5].keys())

    saved_env = {key: os.environ.get(key) for key in all_env_keys}
    try:
        for config_key, env_var, file_val, env_val, default_val, extra_env, file_extra in PRECEDENCE_MATRIX:
            for key in all_env_keys:
                os.environ.pop(key, None)

            config_file.write_text(json.dumps({}), encoding="utf-8")
            cfg = mod.load_config()
            assert _coerce(cfg.get(config_key), default_val), (
                f"{config_key}: expected default {default_val!r}, got {cfg.get(config_key)!r}"
            )

            file_payload = {config_key: file_val, **file_extra}
            config_file.write_text(json.dumps(file_payload), encoding="utf-8")
            cfg = mod.load_config()
            assert _coerce(cfg.get(config_key), file_val), (
                f"{config_key}: expected file value {file_val!r}, got {cfg.get(config_key)!r}"
            )

            os.environ[env_var] = env_val
            for extra_key, extra_val in extra_env.items():
                os.environ[extra_key] = extra_val
            cfg = mod.load_config()
            assert _coerce(cfg.get(config_key), env_val), (
                f"{config_key}: expected env value {env_val!r}, got {cfg.get(config_key)!r}"
            )
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if config_file.exists():
            config_file.unlink()


if __name__ == "__main__":
    try:
        test_precedence_matrix()
        print("PASS test_precedence_matrix")
    except AssertionError as exc:
        print("FAIL test_precedence_matrix", exc)
        sys.exit(1)
