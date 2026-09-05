"""Client-side automatic capture controls. Explicit remember is independent."""

import fnmatch
import json
import os
import re
from pathlib import PurePosixPath

_FALSE = {"0", "false", "no", "off"}
_PATH_KEYS = {"file_path", "filepath", "path", "paths", "notebook_path", "filename"}
_DENY_PATHS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    ".npmrc",
    ".netrc",
    "*/.aws/credentials",
    "*/.ssh/*",
    "*.p12",
    "*.pfx",
    "secrets.*",
    "credentials.*",
)
_SECRET_KEY = re.compile(
    r"(?i)^(?:authorization|x-api-key|(?:.*[_-])?(?:secret|token|password|passwd|api[_-]?key))$"
)
_RULES = (
    (
        "private-key",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
    ),
    ("connection", r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?)://[^\s\"'<>]+"),
    ("authorization", r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+"),
    (
        "credential",
        r"(?i)[\"']?\b(?:[\w-]*[_-])?(?:secret|token|password|passwd|api[_-]?key|x-api-key)"
        r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    ),
    (
        "vendor-key",
        r"\b(?:sk-(?:proj-|ant-)?|gh[pousr]_|github_pat_|xox[baprs]-|whsec_)[A-Za-z0-9_-]{16,}",
    ),
    ("bcrypt", r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}"),
)
_COMPILED = [(kind, re.compile(pattern)) for kind, pattern in _RULES]


def capture_enabled() -> bool:
    return os.environ.get("COGNEE_CAPTURE", "true").strip().lower() not in _FALSE


def _list_env(name: str, *, separator: str = ",") -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        values = json.loads(raw)
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError(f"{name} must be an array of strings")
        return values
    return [part.strip() for part in raw.split(separator) if part.strip()]


def _sensitive_path(value) -> bool:
    if isinstance(value, (tuple, list)):
        return any(_sensitive_path(item) for item in value)
    if not isinstance(value, str):
        return False
    normalized = value.replace("\\", "/").lower()
    name = PurePosixPath(normalized).name
    return any(
        fnmatch.fnmatchcase(name, pattern.lower())
        or fnmatch.fnmatchcase("/" + normalized.lstrip("/"), pattern.lower())
        for pattern in (*_DENY_PATHS, *_list_env("COGNEE_CAPTURE_DENY_PATHS"))
    )


def _has_sensitive_path(value) -> bool:
    if isinstance(value, dict):
        return any(
            (str(key).lower() in _PATH_KEYS and _sensitive_path(item)) or _has_sensitive_path(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_sensitive_path(item) for item in value)
    return False


def allow_tool(name: str, params) -> bool:
    if not capture_enabled():
        return False
    tools = _list_env("COGNEE_CAPTURE_TOOLS", separator="|") or ["*"]
    if not any(fnmatch.fnmatchcase(name, pattern) for pattern in tools):
        return False
    return not _has_sensitive_path(params)


def redact(value):
    """Redact before truncation so a clipped private key cannot escape matching."""
    if os.environ.get("COGNEE_CAPTURE_REDACT", "true").strip().lower() in _FALSE:
        return value
    if isinstance(value, str):
        for kind, pattern in _COMPILED:
            value = pattern.sub(f"[redacted:{kind}]", value)
        # User-supplied regexes are JSON strings; commas are valid regex syntax.
        raw = os.environ.get("COGNEE_CAPTURE_REDACT_PATTERNS", "").strip()
        if raw:
            patterns = _list_env("COGNEE_CAPTURE_REDACT_PATTERNS", separator="\n")
            for pattern in patterns:
                value = re.sub(pattern, "[redacted:custom]", value)
        return value
    if isinstance(value, dict):
        return {
            key: "[redacted:credential]" if _SECRET_KEY.match(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
