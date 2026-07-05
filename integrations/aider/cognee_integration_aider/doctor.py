"""Environment diagnostics for the Cognee Aider integration."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import AiderCogneeConfig, config_path

OK = "ok"
WARN = "warn"
ERROR = "error"


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str
    fix: str = ""

    @property
    def failed(self) -> bool:
        return self.status == ERROR

    @property
    def warned(self) -> bool:
        return self.status == WARN


@dataclass(frozen=True)
class DoctorReport:
    checks: list[DoctorCheck]

    @property
    def has_errors(self) -> bool:
        return any(check.failed for check in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(check.warned for check in self.checks)

    def exit_code(self, *, strict: bool = False) -> int:
        if self.has_errors or (strict and self.has_warnings):
            return 1
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "error" if self.has_errors else "warn" if self.has_warnings else "ok",
            "checks": [asdict(check) for check in self.checks],
        }


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _check_python() -> DoctorCheck:
    version = platform.python_version()
    if sys.version_info >= (3, 10):
        return DoctorCheck("Python", OK, f"Python {version} detected")
    return DoctorCheck(
        "Python",
        ERROR,
        f"Python {version} detected; cognee-integration-aider requires Python >= 3.10",
        "Create a Python 3.10+ environment and reinstall cognee-integration-aider.",
    )


def _check_versions() -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    integration_version = _package_version("cognee-integration-aider")
    if integration_version:
        checks.append(
            DoctorCheck("Aider integration", OK, f"Version {integration_version} installed")
        )
    else:
        checks.append(
            DoctorCheck(
                "Aider integration",
                WARN,
                "Package metadata is not installed; running from source or an editable checkout",
                "Install with `pip install -e .` or `uv sync` if package metadata is needed.",
            )
        )

    cognee_version = _package_version("cognee")
    if cognee_version:
        checks.append(DoctorCheck("Cognee package", OK, f"Version {cognee_version} installed"))
    else:
        checks.append(
            DoctorCheck(
                "Cognee package",
                ERROR,
                "The `cognee` package is not importable from this Python environment",
                "Install this integration's dependencies, for example `uv sync` "
                "or `pip install cognee-integration-aider`.",
            )
        )
    return checks


def _check_config_file(path: Path) -> DoctorCheck:
    if not path.exists():
        return DoctorCheck(
            "Config file",
            WARN,
            f"No config file found at {path}; defaults and environment variables will be used",
            "Create `.aider/cognee.json` if this project needs non-default Cognee settings.",
        )
    if not path.is_file():
        return DoctorCheck(
            "Config file",
            ERROR,
            f"Config path exists but is not a file: {path}",
            "Point AIDER_COGNEE_CONFIG at a JSON file or remove the conflicting path.",
        )
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return DoctorCheck(
            "Config file",
            ERROR,
            f"Config file is not valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}",
            "Fix the JSON syntax or remove AIDER_COGNEE_CONFIG to use defaults.",
        )
    except OSError as exc:
        return DoctorCheck(
            "Config file",
            ERROR,
            f"Config file could not be read: {exc}",
            "Fix file permissions or point AIDER_COGNEE_CONFIG at a readable file.",
        )
    if not isinstance(loaded, dict):
        return DoctorCheck(
            "Config file",
            ERROR,
            "Config file must contain a JSON object",
            "Use key/value JSON such as `{ \"dataset\": \"my-project\" }`.",
        )
    return DoctorCheck("Config file", OK, f"Loaded {path}")


def _check_config_values(config: AiderCogneeConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    if config.dataset.strip():
        checks.append(DoctorCheck("Dataset", OK, f"Using dataset `{config.dataset}`"))
    else:
        checks.append(
            DoctorCheck("Dataset", ERROR, "Dataset is empty", "Set COGNEE_DATASET or `dataset`.")
        )

    if config.top_k >= 1:
        checks.append(DoctorCheck("Top K", OK, f"Using top_k={config.top_k}"))
    else:
        checks.append(
            DoctorCheck(
                "Top K",
                ERROR,
                "top_k must be >= 1",
                "Set COGNEE_TOP_K to 1 or higher.",
            )
        )
    return checks


def _check_env(config: AiderCogneeConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    llm_api_key = os.environ.get("LLM_API_KEY", "").strip()
    if llm_api_key:
        checks.append(DoctorCheck("LLM_API_KEY", OK, "Configured"))
    else:
        checks.append(
            DoctorCheck(
                "LLM_API_KEY",
                WARN,
                "Not configured; writes or graph processing may fail for local Cognee",
                "Export LLM_API_KEY before running remember/cognify flows.",
            )
        )

    if config.service_url:
        checks.append(DoctorCheck("Cognee service URL", OK, f"Using {config.service_url}"))
        if config.api_key:
            checks.append(DoctorCheck("COGNEE_API_KEY", OK, "Configured"))
        else:
            checks.append(
                DoctorCheck(
                    "COGNEE_API_KEY",
                    WARN,
                    "Not configured; remote services that require auth will reject requests",
                    "Export COGNEE_API_KEY if your Cognee service requires authentication.",
                )
            )
    else:
        checks.append(
            DoctorCheck(
                "Cognee service URL",
                WARN,
                "COGNEE_BASE_URL/COGNEE_SERVICE_URL is not set; the integration "
                "will use local Cognee behavior",
                "Set COGNEE_BASE_URL for remote Cognee, or leave unset for local usage.",
            )
        )
    return checks


def _check_path_writable(label: str, raw_path: str) -> DoctorCheck:
    path = Path(raw_path).expanduser()
    target = path if path.exists() else path.parent
    if not target.exists():
        return DoctorCheck(
            label,
            ERROR,
            f"{target} does not exist",
            f"Create the directory or update {label.replace(' ', '_').upper()}.",
        )
    if os.access(target, os.W_OK):
        return DoctorCheck(label, OK, f"{target} is writable")
    return DoctorCheck(
        label,
        ERROR,
        f"{target} is not writable",
        "Fix directory permissions or choose a writable Cognee data location.",
    )


def _check_storage_paths(config: AiderCogneeConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    if config.data_root:
        checks.append(_check_path_writable("Data root", config.data_root))
    else:
        checks.append(
            DoctorCheck(
                "Data root",
                WARN,
                "COGNEE_DATA_ROOT is not set; Cognee will choose its default storage location",
                "Set COGNEE_DATA_ROOT when you need deterministic local storage.",
            )
        )
    if config.system_root:
        checks.append(_check_path_writable("System root", config.system_root))
    return checks


def _check_docker(timeout: float) -> DoctorCheck:
    docker = shutil.which("docker")
    if not docker:
        return DoctorCheck(
            "Docker",
            WARN,
            "Docker CLI was not found; this is only required for Docker-backed setups",
            "Install Docker if your selected database or Cognee deployment uses containers.",
        )
    try:
        subprocess.run(
            [docker, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired:
        return DoctorCheck(
            "Docker",
            WARN,
            f"Docker did not respond within {timeout:g}s",
            "Start Docker and rerun `cognee-aider doctor`.",
        )
    except subprocess.CalledProcessError:
        return DoctorCheck(
            "Docker",
            WARN,
            "Docker CLI is installed but the daemon is not reachable",
            "Start Docker Desktop or your Docker daemon.",
        )
    return DoctorCheck("Docker", OK, "Docker daemon is running")


def _database_urls() -> Iterable[tuple[str, str]]:
    for key in ("DATABASE_URL", "POSTGRES_URL", "POSTGRES_DSN", "NEO4J_URI"):
        value = os.environ.get(key, "").strip()
        if value:
            yield key, value


def _default_port(parsed: urllib.parse.ParseResult) -> int | None:
    if parsed.scheme in {"postgres", "postgresql"}:
        return 5432
    if parsed.scheme in {"neo4j", "bolt"}:
        return 7687
    return None


def _check_database(timeout: float) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    for key, url in _database_urls():
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in {"sqlite", "sqlite3"}:
            db_path = Path(urllib.request.url2pathname(parsed.path)).expanduser()
            checks.append(_check_path_writable(key, str(db_path)))
            continue
        host = parsed.hostname
        port = parsed.port or _default_port(parsed)
        if not host or not port:
            checks.append(
                DoctorCheck(
                    key,
                    WARN,
                    "Database URL is set but host/port could not be determined",
                    "Verify the database URL format before relying on this configuration.",
                )
            )
            continue
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
        except OSError as exc:
            checks.append(
                DoctorCheck(
                    key,
                    WARN,
                    f"Could not open a TCP connection to {host}:{port}: {exc}",
                    "Start the database, fix the host/port, or unset this variable "
                    "if it is not used.",
                )
            )
        else:
            checks.append(DoctorCheck(key, OK, f"TCP connection to {host}:{port} succeeded"))
    if not checks:
        checks.append(
            DoctorCheck(
                "Database",
                WARN,
                "No database connection environment variable detected; Cognee defaults may be used",
                "Set DATABASE_URL/POSTGRES_URL/NEO4J_URI when using an external database.",
            )
        )
    return checks


def _check_service_url(
    config: AiderCogneeConfig, timeout: float, skip_network: bool
) -> DoctorCheck:
    if not config.service_url:
        return DoctorCheck(
            "Cognee service health",
            WARN,
            "Skipped because no Cognee service URL is configured",
            "Set COGNEE_BASE_URL to validate a remote/local Cognee HTTP service.",
        )
    if skip_network:
        return DoctorCheck(
            "Cognee service health",
            WARN,
            "Skipped because network checks are disabled",
            "Rerun without --skip-network to validate the Cognee service endpoint.",
        )

    base = config.service_url.rstrip("/")
    health_urls = [f"{base}/health", f"{base}/api/v1/health"]
    headers = {"User-Agent": "cognee-aider-doctor"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    errors: list[str] = []
    for url in health_urls:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if 200 <= response.status < 500:
                    return DoctorCheck(
                        "Cognee service health",
                        OK if response.status < 400 else WARN,
                        f"{url} returned HTTP {response.status}",
                        "" if response.status < 400 else "Check COGNEE_API_KEY and service logs.",
                    )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            errors.append(f"{url}: {exc}")
    return DoctorCheck(
        "Cognee service health",
        WARN,
        "Could not reach Cognee service health endpoint: " + "; ".join(errors),
        "Start the Cognee service, fix COGNEE_BASE_URL, or rerun with --skip-network.",
    )


def run_doctor(
    config: AiderCogneeConfig,
    *,
    timeout: float = 2.0,
    skip_network: bool = False,
    cwd: str | Path | None = None,
) -> DoctorReport:
    path = config_path(cwd)
    checks = [
        _check_python(),
        *_check_versions(),
        _check_config_file(path),
        *_check_config_values(config),
        *_check_env(config),
        *_check_storage_paths(config),
        _check_docker(timeout),
        *_check_database(timeout),
        _check_service_url(config, timeout, skip_network),
    ]
    return DoctorReport(checks)


def render_report(report: DoctorReport, *, strict: bool = False) -> str:
    icon = {OK: "OK", WARN: "WARN", ERROR: "FAIL"}
    lines: list[str] = []
    for check in report.checks:
        lines.append(f"[{icon[check.status]}] {check.name}: {check.detail}")
        if check.fix:
            lines.append(f"      fix: {check.fix}")
    lines.append("")
    if report.has_errors:
        lines.append("Cognee environment has blocking issues.")
    elif strict and report.has_warnings:
        lines.append("Cognee environment has warnings and --strict was requested.")
    elif report.has_warnings:
        lines.append("Cognee environment is usable, with warnings.")
    else:
        lines.append("Cognee environment looks healthy.")
    return "\n".join(lines)
