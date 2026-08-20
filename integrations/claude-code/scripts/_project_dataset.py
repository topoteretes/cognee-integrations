from __future__ import annotations

import hashlib
import re
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

GIT_TIMEOUT_SECONDS = 1.0
_SUPPORTED_SCHEMES = {"git", "http", "https", "ssh"}
_SCP_REMOTE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>\[[^\]]+\]|[^:/\s]+):(?P<path>.+)$")


def _repository_path(value: str) -> str:
    path = str(value or "").strip().strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return path.strip("/")


def _format_host(value: str, port: int | None = None) -> str:
    host = str(value or "").strip().strip("[]").lower()
    if not host:
        return ""
    formatted = f"[{host}]" if ":" in host else host
    return f"{formatted}:{port}" if port is not None else formatted


def normalize_git_remote(remote: str) -> str | None:
    value = str(remote or "").strip()
    if not value:
        return None
    if "://" not in value:
        match = _SCP_REMOTE.fullmatch(value)
        if not match or (
            len(match["host"]) == 1
            and (
                match["host"].isascii()
                and match["host"].isalpha()
                or match["path"].startswith(("\\", "/"))
            )
        ):
            return None
        host = _format_host(match["host"])
        path = _repository_path(match["path"])
        return f"git:{host}/{path}" if host and path else None
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in _SUPPORTED_SCHEMES or not parsed.hostname:
            return None
        host = parsed.hostname.lower()
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if scheme == "ssh" and port == 22:
        port = None
    host_part = _format_host(host, port)
    path = _repository_path(parsed.path)
    return f"git:{host_part}/{path}" if path else None


def _slug(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", str(value or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")[:32].rstrip("-")
    return slug or "workspace"


def dataset_name(identity: str, slug_source: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"project_{_slug(slug_source)}_{digest}"


def _run_git(workspace: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (FileNotFoundError, OSError, UnicodeError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _canonical_dir(value: str | Path, *, relative_to: Path | None = None) -> Path | None:
    try:
        path = Path(value).expanduser()
        if relative_to is not None and not path.is_absolute():
            path = relative_to / path
        path = path.resolve(strict=True)
        return path if path.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def derive_project_dataset(workspace: str) -> str | None:
    root = _canonical_dir(workspace)
    if root is None:
        return None
    top_raw = _run_git(root, "rev-parse", "--show-toplevel")
    top = _canonical_dir(top_raw) if top_raw else None
    if top is not None:
        normalized = normalize_git_remote(_run_git(root, "config", "--get", "remote.origin.url"))
        if normalized:
            return dataset_name(normalized, normalized.rsplit("/", 1)[-1])
        common_raw = _run_git(root, "rev-parse", "--git-common-dir")
        common = _canonical_dir(common_raw, relative_to=root) if common_raw else None
        if common is not None:
            slug_source = (
                common.parent.name if common.name == ".git" else common.name.removesuffix(".git")
            )
            return dataset_name(f"gitdir:{common}", slug_source)
    return dataset_name(f"workspace:{root}", root.name)
