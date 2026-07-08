"""Project/session scoping for Aider memory."""

from __future__ import annotations

import re
from hashlib import sha256
from pathlib import Path

from .config import AiderCogneeConfig

_SAFE_CHARS = re.compile(r"[^a-zA-Z0-9_.:-]+")


def _slug(value: str) -> str:
    cleaned = _SAFE_CHARS.sub("-", value.strip()).strip("-").lower()
    return cleaned or "project"


def find_project_root(path: str | Path | None = None) -> Path:
    current = Path(path or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def project_id_from_path(path: str | Path | None = None) -> str:
    root = find_project_root(path)
    digest = sha256(str(root).encode("utf-8")).hexdigest()[:8]
    return f"{_slug(root.name)}-{digest}"


def build_session_id(
    config: AiderCogneeConfig | None = None,
    *,
    cwd: str | Path | None = None,
    session_id: str | None = None,
) -> str:
    cfg = config or AiderCogneeConfig()
    project_id = cfg.project_id or project_id_from_path(cwd)
    suffix = _slug(session_id) if session_id else "default"
    return f"{_slug(cfg.session_prefix)}:{_slug(project_id)}:{suffix}"
