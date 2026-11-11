"""Configuration and trusted scope for the Browser Use adapter.

Precedence, field by field: explicit constructor value -> environment
(``COGNEE_BASE_URL`` / ``COGNEE_API_KEY`` plus ``COGNEE_BROWSER_USE_*``) -> safe
default. The adapter never scans a user's existing agent configuration.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_DATASET_PREFIX = "browser_use_"

DEFAULT_RECALL_TOP_K = 5
DEFAULT_RECALL_MAX_CHARS = 6_000
DEFAULT_RECALL_TIMEOUT_S = 8.0
DEFAULT_REQUEST_TIMEOUT_S = 15.0
DEFAULT_WRITE_TIMEOUT_S = 30.0
DEFAULT_STATUS_TIMEOUT_S = 10.0
DEFAULT_MAX_SUMMARY_CHARS = 4_000
DEFAULT_MAX_SOURCE_URLS = 20
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_TOOL_CONTENT_CHARS = 2_000
DEFAULT_MAX_REMEMBER_CALLS_PER_RUN = 3
# Each new document is a cognify pass billed to the tenant's key, so the per-run
# budget has a ceiling as well as a default.
MAX_REMEMBER_CALLS_PER_RUN_CAP = 20

_DATASET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ConfigError(ValueError):
    """Raised when the adapter cannot be configured safely."""


@dataclass(frozen=True)
class MemoryScope:
    """A trusted scope supplied by application code, never by the model.

    Any subset of the three ids may be set; the opaque ``key`` is stable for the
    same combination, so a dataset name reveals nothing about the ids and two
    different scopes never share a dataset. Dataset naming organizes storage; it
    is not an access-control guarantee - the hosting application authenticates
    callers and the backend enforces its own credential permissions.
    """

    tenant_id: Optional[str] = None
    user_id: Optional[str] = None
    project_id: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return not (self.tenant_id or self.user_id or self.project_id)

    @property
    def key(self) -> str:
        raw = "\x1f".join(
            [
                "tenant=" + (self.tenant_id or ""),
                "user=" + (self.user_id or ""),
                "project=" + (self.project_id or ""),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def dataset_name(self, prefix: str = DEFAULT_DATASET_PREFIX) -> str:
        return f"{prefix}{self.key}"


def _env_str(env: Mapping[str, str], key: str) -> Optional[str]:
    value = env.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_float(env: Mapping[str, str], key: str) -> Optional[float]:
    raw = _env_str(env, key)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{key} must not be negative")
    return value


def _env_int(env: Mapping[str, str], key: str) -> Optional[int]:
    raw = _env_str(env, key)
    if raw is None:
        return None
    if not raw.isdigit():
        raise ConfigError(f"{key} must be a non-negative integer, got {raw!r}")
    return int(raw)


def _env_bool(env: Mapping[str, str], key: str) -> Optional[bool]:
    raw = _env_str(env, key)
    if raw is None:
        return None
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class CogneeBrowserMemoryConfig:
    """Resolved adapter configuration. Build it with :func:`resolve_config`."""

    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    #: A single configured dataset. When unset, every run must carry a
    #: :class:`MemoryScope` from which the dataset is derived.
    dataset: Optional[str] = None
    dataset_prefix: str = DEFAULT_DATASET_PREFIX

    #: Automatic recall before the run (bounded, degrades to empty context).
    recall_enabled: bool = True
    recall_top_k: int = DEFAULT_RECALL_TOP_K
    recall_max_chars: int = DEFAULT_RECALL_MAX_CHARS
    recall_timeout_s: float = DEFAULT_RECALL_TIMEOUT_S

    #: Final-outcome capture after the run.
    capture_enabled: bool = True
    #: The model-facing ``cognee_remember`` action. Opt-in: model-proposed facts
    #: are stored as unverified observations.
    remember_tool_enabled: bool = False
    #: Upper bound on new documents ``cognee_remember`` may create in one run.
    #: Each one is a background cognify pass on the tenant's LLM key; the
    #: final-outcome record is not counted. Deduplicated repeats are free.
    max_remember_calls_per_run: int = DEFAULT_MAX_REMEMBER_CALLS_PER_RUN
    max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS
    max_source_urls: int = DEFAULT_MAX_SOURCE_URLS
    max_tool_content_chars: int = DEFAULT_MAX_TOOL_CONTENT_CHARS

    #: ``None`` keeps receipts in memory (reported as non-durable).
    receipts_path: Optional[str] = None

    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    write_timeout_s: float = DEFAULT_WRITE_TIMEOUT_S
    status_timeout_s: float = DEFAULT_STATUS_TIMEOUT_S
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    extra: Mapping[str, Any] = field(default_factory=dict)

    def with_overrides(self, **overrides: Any) -> "CogneeBrowserMemoryConfig":
        return replace(self, **{k: v for k, v in overrides.items() if v is not None})

    def validate(self) -> None:
        problems = []
        if not re.match(r"^https?://", self.base_url):
            problems.append(
                f"COGNEE_BASE_URL must start with http:// or https:// ({self.base_url!r})"
            )
        if not self.api_key:
            problems.append("COGNEE_API_KEY is required: the adapter authenticates with X-Api-Key")
        if self.dataset is not None and not _DATASET_NAME.match(self.dataset):
            problems.append(f"dataset {self.dataset!r} is not a valid dataset name")
        if not _DATASET_NAME.match(self.dataset_prefix + "x"):
            problems.append(f"dataset_prefix {self.dataset_prefix!r} is not a valid dataset prefix")
        if self.recall_top_k < 1:
            problems.append("recall_top_k must be at least 1")
        if self.recall_max_chars < 100:
            problems.append("recall_max_chars must be at least 100")
        if not 0 <= self.max_remember_calls_per_run <= MAX_REMEMBER_CALLS_PER_RUN_CAP:
            problems.append(
                "max_remember_calls_per_run must be between 0 and "
                f"{MAX_REMEMBER_CALLS_PER_RUN_CAP} (each new document is a cognify pass)"
            )
        if problems:
            raise ConfigError("\n".join(problems))

    def redacted(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        data["api_key"] = "<set>" if self.api_key else "<missing>"
        return data


def resolve_config(
    env: Optional[Mapping[str, str]] = None, **explicit: Any
) -> CogneeBrowserMemoryConfig:
    """Resolve a config: explicit keyword values win, then the environment, then defaults."""
    env = os.environ if env is None else env
    from_env: dict[str, Any] = {
        "base_url": _env_str(env, "COGNEE_BASE_URL"),
        "api_key": _env_str(env, "COGNEE_API_KEY"),
        "dataset": _env_str(env, "COGNEE_BROWSER_USE_DATASET"),
        "dataset_prefix": _env_str(env, "COGNEE_BROWSER_USE_DATASET_PREFIX"),
        "recall_enabled": _env_bool(env, "COGNEE_BROWSER_USE_RECALL"),
        "recall_top_k": _env_int(env, "COGNEE_BROWSER_USE_RECALL_TOP_K"),
        "recall_max_chars": _env_int(env, "COGNEE_BROWSER_USE_RECALL_MAX_CHARS"),
        "recall_timeout_s": _env_float(env, "COGNEE_BROWSER_USE_RECALL_TIMEOUT_S"),
        "capture_enabled": _env_bool(env, "COGNEE_BROWSER_USE_CAPTURE"),
        "remember_tool_enabled": _env_bool(env, "COGNEE_BROWSER_USE_REMEMBER_TOOL"),
        "max_remember_calls_per_run": _env_int(env, "COGNEE_BROWSER_USE_MAX_REMEMBER_CALLS"),
        "max_summary_chars": _env_int(env, "COGNEE_BROWSER_USE_MAX_SUMMARY_CHARS"),
        "receipts_path": _env_str(env, "COGNEE_BROWSER_USE_RECEIPTS_PATH"),
        "request_timeout_s": _env_float(env, "COGNEE_BROWSER_USE_REQUEST_TIMEOUT_S"),
        "write_timeout_s": _env_float(env, "COGNEE_BROWSER_USE_WRITE_TIMEOUT_S"),
    }
    unknown = set(explicit) - set(CogneeBrowserMemoryConfig.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"unknown configuration keys: {sorted(unknown)}")
    merged = {k: v for k, v in from_env.items() if v is not None}
    merged.update({k: v for k, v in explicit.items() if v is not None})
    if "base_url" in merged:
        merged["base_url"] = str(merged["base_url"]).rstrip("/")
    config = CogneeBrowserMemoryConfig(**merged)
    config.validate()
    return config
