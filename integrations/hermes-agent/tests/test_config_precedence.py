"""Config precedence matrix: env > config file > defaults (config.py)."""

from __future__ import annotations

import json
import os

import pytest

from cognee_integration_hermes.config import _DEFAULTS, _ENV_MAP, load_config, save_config

PRECEDENCE_MATRIX = [
    ("dataset", "COGNEE_DATASET", "file_dataset", "env_dataset"),
    ("top_k", "COGNEE_TOP_K", "7", "9"),
    ("auto_route", "COGNEE_AUTO_ROUTE", "false", "true"),
    ("improve_on_end", "COGNEE_IMPROVE_ON_END", "false", "true"),
    ("improve_background", "COGNEE_IMPROVE_BACKGROUND", "false", "true"),
    ("session_prefix", "COGNEE_SESSION_PREFIX", "file_prefix", "env_prefix"),
    ("service_url", "COGNEE_BASE_URL", "https://file.example", "https://env.example"),
    ("api_key", "COGNEE_API_KEY", "file-key", "env-key"),
    ("llm_api_key", "LLM_API_KEY", "file-llm", "env-llm"),
    ("llm_model", "LLM_MODEL", "file-model", "env-model"),
    ("embedded", "COGNEE_EMBEDDED", "false", "true"),
    ("local_port", "COGNEE_LOCAL_PORT", "9001", "9002"),
    ("server_boot_timeout", "COGNEE_SERVER_BOOT_TIMEOUT", "45", "55"),
    ("data_root", "COGNEE_DATA_ROOT", "/file/data", "/env/data"),
    ("system_root", "COGNEE_SYSTEM_ROOT", "/file/system", "/env/system"),
    (
        "identity_email",
        "COGNEE_HERMES_USER_EMAIL",
        "file@example.com",
        "env@example.com",
    ),
    (
        "identity_password",
        "COGNEE_HERMES_USER_PASSWORD",
        "file-pass",
        "env-pass",
    ),
    ("recall_timeout", "COGNEE_RECALL_TIMEOUT", "70", "80"),
    ("write_timeout", "COGNEE_WRITE_TIMEOUT", "130", "140"),
    ("improve_timeout", "COGNEE_IMPROVE_TIMEOUT", "310", "320"),
]


def _coerce_boolish(actual, expected_str: str) -> bool:
    if isinstance(actual, bool):
        return str(actual).lower() == expected_str.lower()
    return str(actual).lower() == expected_str.lower()


@pytest.mark.parametrize(
    "config_key,env_var,file_val,env_val",
    PRECEDENCE_MATRIX,
    ids=[row[0] for row in PRECEDENCE_MATRIX],
)
def test_precedence_matrix(config_key, env_var, file_val, env_val, tmp_path, monkeypatch):
    for key in _ENV_MAP:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("COGNEE_SERVICE_URL", raising=False)

    cfg = load_config(tmp_path)
    assert cfg[config_key] == _DEFAULTS[config_key]

    save_config({config_key: file_val}, tmp_path)
    cfg = load_config(tmp_path)
    expected_file = file_val
    if config_key in {"top_k", "local_port", "server_boot_timeout", "recall_timeout", "write_timeout", "improve_timeout"}:
        expected_file = int(file_val)
    elif config_key in {"auto_route", "improve_on_end", "embedded"}:
        assert _coerce_boolish(cfg[config_key], file_val)
        monkeypatch.setenv(env_var, env_val)
        cfg = load_config(tmp_path)
        assert _coerce_boolish(cfg[config_key], env_val)
        return

    assert cfg[config_key] == expected_file

    monkeypatch.setenv(env_var, env_val)
    cfg = load_config(tmp_path)
    if config_key in {"top_k", "local_port", "server_boot_timeout", "recall_timeout", "write_timeout", "improve_timeout"}:
        assert cfg[config_key] == int(env_val)
    else:
        assert cfg[config_key] == env_val


def test_env_overrides_file_service_url(tmp_path, monkeypatch):
    monkeypatch.delenv("COGNEE_SERVICE_URL", raising=False)
    monkeypatch.setenv("COGNEE_BASE_URL", "https://from-env.example")
    save_config({"service_url": "https://from-file.example"}, tmp_path)
    assert load_config(tmp_path)["service_url"] == "https://from-env.example"


def test_base_url_preferred_over_service_url(tmp_path, monkeypatch):
    monkeypatch.delenv("COGNEE_BASE_URL", raising=False)
    monkeypatch.setenv("COGNEE_SERVICE_URL", "https://legacy.example")
    save_config({"service_url": "https://file.example"}, tmp_path)
    assert load_config(tmp_path)["service_url"] == "https://legacy.example"

    monkeypatch.setenv("COGNEE_BASE_URL", "https://canonical.example")
    assert load_config(tmp_path)["service_url"] == "https://canonical.example"
