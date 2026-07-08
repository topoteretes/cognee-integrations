"""Configuration helpers for the Cognee Aider integration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_DATASET = "aider"
DEFAULT_SESSION_PREFIX = "aider"


@dataclass(frozen=True)
class AiderCogneeConfig:
    dataset: str = DEFAULT_DATASET
    session_prefix: str = DEFAULT_SESSION_PREFIX
    project_id: str = ""
    top_k: int = 5
    self_improvement: bool = False
    service_url: str = ""
    api_key: str = ""
    data_root: str = ""
    system_root: str = ""


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


def config_path(cwd: str | Path | None = None) -> Path:
    env_path = os.environ.get("AIDER_COGNEE_CONFIG", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    root = Path(cwd or Path.cwd()).expanduser()
    return root / ".aider" / "cognee.json"


def _load_file_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def load_config(cwd: str | Path | None = None) -> AiderCogneeConfig:
    values: dict[str, Any] = {
        "dataset": DEFAULT_DATASET,
        "session_prefix": DEFAULT_SESSION_PREFIX,
        "project_id": "",
        "top_k": 5,
        "self_improvement": False,
        "service_url": "",
        "api_key": "",
        "data_root": "",
        "system_root": "",
    }

    file_values = {
        key: value
        for key, value in _load_file_config(config_path(cwd)).items()
        if value is not None
    }
    values.update(file_values)

    env_map = {
        "COGNEE_DATASET": "dataset",
        "COGNEE_SESSION_PREFIX": "session_prefix",
        "COGNEE_PROJECT_ID": "project_id",
        "COGNEE_TOP_K": "top_k",
        "COGNEE_SELF_IMPROVEMENT": "self_improvement",
        "COGNEE_API_KEY": "api_key",
        "COGNEE_DATA_ROOT": "data_root",
        "COGNEE_SYSTEM_ROOT": "system_root",
    }
    for env_key, config_key in env_map.items():
        if os.environ.get(env_key, "") != "":
            values[config_key] = os.environ[env_key]

    if os.environ.get("COGNEE_SERVICE_URL", "") != "":
        values["service_url"] = os.environ["COGNEE_SERVICE_URL"]
    if os.environ.get("COGNEE_BASE_URL", "") != "":
        values["service_url"] = os.environ["COGNEE_BASE_URL"]

    values["top_k"] = max(1, str_to_int(values.get("top_k"), 5))
    values["self_improvement"] = str_to_bool(values.get("self_improvement"), False)

    return AiderCogneeConfig(**values)
