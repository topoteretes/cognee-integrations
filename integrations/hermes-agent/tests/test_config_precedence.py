import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cognee_integration_hermes.config import (
    DEFAULT_DATASET,
    DEFAULT_IDENTITY_EMAIL,
    DEFAULT_IDENTITY_PASSWORD,
    load_config,
)


ENV_MATRIX = {
    "llm_api_key": ("LLM_API_KEY", "env-llm-key", "file-llm-key", ""),
    "llm_model": ("LLM_MODEL", "env-model", "file-model", ""),
    "service_url": ("COGNEE_BASE_URL", "https://env.example", "https://file.example", ""),
    "api_key": ("COGNEE_API_KEY", "env-api-key", "file-api-key", ""),
    "embedded": ("COGNEE_EMBEDDED", "true", False, False),
    "local_port": ("COGNEE_LOCAL_PORT", "8123", 8122, 8000),
    "server_boot_timeout": ("COGNEE_SERVER_BOOT_TIMEOUT", "44", 33, 30),
    "dataset": ("COGNEE_DATASET", "env-dataset", "file-dataset", DEFAULT_DATASET),
    "top_k": ("COGNEE_TOP_K", "9", 8, 5),
    "auto_route": ("COGNEE_AUTO_ROUTE", "false", True, True),
    "improve_on_end": ("COGNEE_IMPROVE_ON_END", "false", True, True),
    "improve_background": ("COGNEE_IMPROVE_BACKGROUND", "true", "false", ""),
    "session_prefix": ("COGNEE_SESSION_PREFIX", "env-prefix", "file-prefix", "hermes"),
    "data_root": ("COGNEE_DATA_ROOT", "/env/data", "/file/data", ""),
    "system_root": ("COGNEE_SYSTEM_ROOT", "/env/system", "/file/system", ""),
    "identity_email": (
        "COGNEE_HERMES_USER_EMAIL",
        "env@example.com",
        "file@example.com",
        DEFAULT_IDENTITY_EMAIL,
    ),
    "identity_password": (
        "COGNEE_HERMES_USER_PASSWORD",
        "env-password",
        "file-password",
        DEFAULT_IDENTITY_PASSWORD,
    ),
    "recall_timeout": ("COGNEE_RECALL_TIMEOUT", "61", 62, 60),
    "write_timeout": ("COGNEE_WRITE_TIMEOUT", "121", 122, 120),
    "improve_timeout": ("COGNEE_IMPROVE_TIMEOUT", "301", 302, 300),
}


def test_defaults_when_no_env_or_config(tmp_path, monkeypatch):
    for env_key, *_ in ENV_MATRIX.values():
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.delenv("COGNEE_SERVICE_URL", raising=False)

    cfg = load_config(tmp_path)

    for key, (_, _, _, expected_default) in ENV_MATRIX.items():
        assert cfg[key] == expected_default


def test_config_file_overrides_defaults(tmp_path, monkeypatch):
    for env_key, *_ in ENV_MATRIX.values():
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.delenv("COGNEE_SERVICE_URL", raising=False)

    file_values = {key: file_value for key, (_, _, file_value, _) in ENV_MATRIX.items()}
    (tmp_path / "cognee.json").write_text(json.dumps(file_values), encoding="utf-8")

    cfg = load_config(tmp_path)

    for key, (_, _, expected_file, _) in ENV_MATRIX.items():
        assert cfg[key] == expected_file


def test_env_overrides_config_file(tmp_path, monkeypatch):
    file_values = {key: file_value for key, (_, _, file_value, _) in ENV_MATRIX.items()}
    (tmp_path / "cognee.json").write_text(json.dumps(file_values), encoding="utf-8")

    for _, (env_key, env_value, _, _) in ENV_MATRIX.items():
        monkeypatch.setenv(env_key, env_value)

    cfg = load_config(tmp_path)

    expected = {
        "embedded": True,
        "local_port": 8123,
        "server_boot_timeout": 44,
        "top_k": 9,
        "auto_route": False,
        "improve_on_end": False,
        "recall_timeout": 61,
        "write_timeout": 121,
        "improve_timeout": 301,
    }
    for key, (_, env_value, _, _) in ENV_MATRIX.items():
        assert cfg[key] == expected.get(key, env_value)


def test_base_url_precedence_over_legacy_service_url(tmp_path, monkeypatch):
    (tmp_path / "cognee.json").write_text(
        json.dumps({"service_url": "https://file.example"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("COGNEE_SERVICE_URL", "https://legacy.example")
    monkeypatch.setenv("COGNEE_BASE_URL", "https://canonical.example")

    assert load_config(tmp_path)["service_url"] == "https://canonical.example"


def test_empty_env_values_do_not_override_config_file(tmp_path, monkeypatch):
    (tmp_path / "cognee.json").write_text(
        json.dumps({"service_url": "https://file.example", "dataset": "file-dataset"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("COGNEE_BASE_URL", "")
    monkeypatch.setenv("COGNEE_DATASET", "")

    cfg = load_config(tmp_path)

    assert cfg["service_url"] == "https://file.example"
    assert cfg["dataset"] == "file-dataset"
