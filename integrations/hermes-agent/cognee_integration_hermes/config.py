"""Configuration helpers for the Cognee Hermes plugin."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_DATASET = "hermes"
# Port for the local cognee server this plugin spawns and owns. 8011 matches the
# other cognee agent plugins (claude-code, codex, openclaw) and deliberately avoids
# cognee's own default of 8000, so we never attach to — or contend with — a server
# the user is running themselves.
DEFAULT_LOCAL_PORT = 8011
DEFAULT_IDENTITY_EMAIL = "hermes-agent@cognee.local"
DEFAULT_IDENTITY_PASSWORD = "hermes-agent-plugin"


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


def resolve_local_roots(
    config: dict[str, Any], hermes_home: str | Path | None = None
) -> tuple[str, str]:
    """Where cognee should keep its data and system directories.

    Explicit ``COGNEE_DATA_ROOT`` / ``COGNEE_SYSTEM_ROOT`` win. Otherwise both are
    scoped under ``HERMES_HOME``, which Hermes requires of plugin storage: two
    profiles must not share one store, and ``hermes backup`` only walks
    ``HERMES_HOME``.

    Returns ``("", "")`` when there is nothing to say — no explicit config and no
    resolvable home — leaving cognee's own defaults in place.

    Note this is per *profile*, not per process. A cognee server is shared by
    whoever reaches its port first, so two profiles on the same
    ``COGNEE_LOCAL_PORT`` still end up on one store; give them separate ports to
    keep them apart.
    """
    home = resolve_hermes_home(hermes_home)
    data_root = str(config.get("data_root") or "")
    system_root = str(config.get("system_root") or "")
    if home is not None:
        if not data_root:
            data_root = str(home / "cognee" / "data")
        if not system_root:
            system_root = str(home / "cognee" / "system")
    return data_root, system_root


def load_config(hermes_home: str | Path | None = None) -> dict[str, Any]:
    """Load plugin config from environment variables and HERMES_HOME/cognee.json."""
    # COGNEE_BASE_URL is the canonical name; COGNEE_SERVICE_URL is a deprecated alias
    # kept for backward compatibility. A set service_url selects remote/cloud mode.
    service_url = os.environ.get("COGNEE_BASE_URL") or os.environ.get("COGNEE_SERVICE_URL", "")
    config: dict[str, Any] = {
        "llm_api_key": os.environ.get("LLM_API_KEY", ""),
        "llm_model": os.environ.get("LLM_MODEL", ""),
        "service_url": service_url,
        "api_key": os.environ.get("COGNEE_API_KEY", ""),
        # Connection mode knobs (see provider.initialize / README "Modes").
        # embedded=true runs cognee in-process (single-process/offline only);
        # otherwise local mode ensures a local server on local_port (DB-safe).
        "embedded": str_to_bool(os.environ.get("COGNEE_EMBEDDED"), False),
        # Which transport reaches cognee: "" / "sdk" (default) or "http" for the
        # direct REST client the other cognee plugins use. See backend.build_backend.
        "transport": os.environ.get("COGNEE_TRANSPORT", ""),
        "local_port": str_to_int(os.environ.get("COGNEE_LOCAL_PORT"), DEFAULT_LOCAL_PORT),
        "server_boot_timeout": str_to_int(os.environ.get("COGNEE_SERVER_BOOT_TIMEOUT"), 30),
        "dataset": os.environ.get("COGNEE_DATASET", DEFAULT_DATASET),
        "top_k": str_to_int(os.environ.get("COGNEE_TOP_K"), 5),
        "auto_route": str_to_bool(os.environ.get("COGNEE_AUTO_ROUTE"), True),
        "improve_on_end": str_to_bool(os.environ.get("COGNEE_IMPROVE_ON_END"), True),
        # Tri-state: "" = auto (background only in server/remote mode, where the
        # server outlives this process; synchronous in embedded). Set to force.
        "improve_background": os.environ.get("COGNEE_IMPROVE_BACKGROUND", ""),
        "session_prefix": os.environ.get("COGNEE_SESSION_PREFIX", "hermes"),
        "data_root": os.environ.get("COGNEE_DATA_ROOT", ""),
        "system_root": os.environ.get("COGNEE_SYSTEM_ROOT", ""),
        "identity_email": os.environ.get("COGNEE_HERMES_USER_EMAIL", DEFAULT_IDENTITY_EMAIL),
        "identity_password": os.environ.get(
            "COGNEE_HERMES_USER_PASSWORD",
            DEFAULT_IDENTITY_PASSWORD,
        ),
        "recall_timeout": str_to_int(os.environ.get("COGNEE_RECALL_TIMEOUT"), 120),
        "write_timeout": str_to_int(os.environ.get("COGNEE_WRITE_TIMEOUT"), 120),
        "improve_timeout": str_to_int(os.environ.get("COGNEE_IMPROVE_TIMEOUT"), 300),
    }

    path = config_path(hermes_home)
    if path and path.exists():
        try:
            file_config = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(file_config, dict):
                config.update(
                    {key: value for key, value in file_config.items() if value is not None}
                )
        except Exception:
            pass

    config["top_k"] = max(1, str_to_int(config.get("top_k"), 5))
    config["recall_timeout"] = max(1, str_to_int(config.get("recall_timeout"), 120))
    config["write_timeout"] = max(1, str_to_int(config.get("write_timeout"), 120))
    config["improve_timeout"] = max(1, str_to_int(config.get("improve_timeout"), 300))
    config["local_port"] = min(
        65535, max(1, str_to_int(config.get("local_port"), DEFAULT_LOCAL_PORT))
    )
    config["server_boot_timeout"] = max(1, str_to_int(config.get("server_boot_timeout"), 30))
    config["auto_route"] = str_to_bool(config.get("auto_route"), True)
    config["improve_on_end"] = str_to_bool(config.get("improve_on_end"), True)
    config["embedded"] = str_to_bool(config.get("embedded"), False)
    return config


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
