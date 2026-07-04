"""Configuration helpers for the Cognee Hermes plugin."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_DATASET = "hermes"
DEFAULT_IDENTITY_EMAIL = "hermes-agent@cognee.local"
DEFAULT_IDENTITY_PASSWORD = "hermes-agent-plugin"

_DEFAULTS: dict[str, Any] = {
    "llm_api_key": "",
    "llm_model": "",
    "service_url": "",
    "api_key": "",
    "embedded": False,
    "local_port": 8000,
    "server_boot_timeout": 30,
    "dataset": DEFAULT_DATASET,
    "top_k": 5,
    "auto_route": True,
    "improve_on_end": True,
    "improve_background": "",
    "session_prefix": "hermes",
    "data_root": "",
    "system_root": "",
    "identity_email": DEFAULT_IDENTITY_EMAIL,
    "identity_password": DEFAULT_IDENTITY_PASSWORD,
    "recall_timeout": 60,
    "write_timeout": 120,
    "improve_timeout": 300,
}

# Env var overrides (env var name → config key). COGNEE_SERVICE_URL is handled
# separately as a deprecated alias for COGNEE_BASE_URL.
_ENV_MAP: dict[str, str] = {
    "LLM_API_KEY": "llm_api_key",
    "LLM_MODEL": "llm_model",
    "COGNEE_BASE_URL": "service_url",
    "COGNEE_API_KEY": "api_key",
    "COGNEE_EMBEDDED": "embedded",
    "COGNEE_LOCAL_PORT": "local_port",
    "COGNEE_SERVER_BOOT_TIMEOUT": "server_boot_timeout",
    "COGNEE_DATASET": "dataset",
    "COGNEE_TOP_K": "top_k",
    "COGNEE_AUTO_ROUTE": "auto_route",
    "COGNEE_IMPROVE_ON_END": "improve_on_end",
    "COGNEE_IMPROVE_BACKGROUND": "improve_background",
    "COGNEE_SESSION_PREFIX": "session_prefix",
    "COGNEE_DATA_ROOT": "data_root",
    "COGNEE_SYSTEM_ROOT": "system_root",
    "COGNEE_HERMES_USER_EMAIL": "identity_email",
    "COGNEE_HERMES_USER_PASSWORD": "identity_password",
    "COGNEE_RECALL_TIMEOUT": "recall_timeout",
    "COGNEE_WRITE_TIMEOUT": "write_timeout",
    "COGNEE_IMPROVE_TIMEOUT": "improve_timeout",
}


def str_to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def str_to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def resolve_hermes_home(hermes_home: str | Path | None = None) -> Path | None:
    if hermes_home:
        return Path(hermes_home).expanduser()
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()).expanduser()
    except Exception:
        return None


def config_path(hermes_home: str | Path | None = None) -> Path | None:
    home = resolve_hermes_home(hermes_home)
    return home / "cognee.json" if home else None


def _apply_env_overrides(config: dict[str, Any]) -> None:
    for env_key, config_key in _ENV_MAP.items():
        val = os.environ.get(env_key, "")
        if val:
            config[config_key] = val

    # Deprecated alias — only when canonical COGNEE_BASE_URL is unset.
    if not os.environ.get("COGNEE_BASE_URL", ""):
        legacy = os.environ.get("COGNEE_SERVICE_URL", "")
        if legacy:
            config["service_url"] = legacy


def _normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    config["top_k"] = max(1, str_to_int(config.get("top_k"), 5))
    config["recall_timeout"] = max(1, str_to_int(config.get("recall_timeout"), 60))
    config["write_timeout"] = max(1, str_to_int(config.get("write_timeout"), 120))
    config["improve_timeout"] = max(1, str_to_int(config.get("improve_timeout"), 300))
    config["local_port"] = min(65535, max(1, str_to_int(config.get("local_port"), 8000)))
    config["server_boot_timeout"] = max(1, str_to_int(config.get("server_boot_timeout"), 30))
    config["auto_route"] = str_to_bool(config.get("auto_route"), True)
    config["improve_on_end"] = str_to_bool(config.get("improve_on_end"), True)
    config["embedded"] = str_to_bool(config.get("embedded"), False)
    return config


def load_config(hermes_home: str | Path | None = None) -> dict[str, Any]:
    """Load merged config: defaults → file → env vars."""
    config = dict(_DEFAULTS)

    path = config_path(hermes_home)
    if path and path.exists():
        try:
            file_config = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(file_config, dict):
                config.update(
                    {
                        key: value
                        for key, value in file_config.items()
                        if value is not None and value != ""
                    }
                )
        except Exception:
            pass

    _apply_env_overrides(config)
    return _normalize_config(config)


def save_config(values: dict[str, Any], hermes_home: str | Path) -> Path:
    """Merge non-secret values into HERMES_HOME/cognee.json."""
    path = config_path(hermes_home)
    if path is None:
        raise RuntimeError("Could not resolve HERMES_HOME.")
    path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}

    existing.update({key: value for key, value in values.items() if value is not None})
    path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_env_vars(env_path: Path, values: dict[str, str]) -> None:
    """Append or update environment variables in a Hermes .env file."""
    if not values:
        return

    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []

    updated: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in values:
            new_lines.append(f"{key}={values[key]}")
            updated.add(key)
        else:
            new_lines.append(line)

    for key, value in values.items():
        if key not in updated:
            new_lines.append(f"{key}={value}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
