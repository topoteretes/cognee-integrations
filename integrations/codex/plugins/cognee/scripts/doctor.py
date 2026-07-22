#!/usr/bin/env python3
"""Cognee Doctor — unified diagnostics for the Codex plugin.

Read-only command that aggregates configuration, connectivity, and
circuit-breaker state into a single diagnostic report.

Usage:
    python doctor.py           # human-readable table
    python doctor.py --json    # machine-readable JSON

Never modifies configuration, initialises databases, registers
resources, writes files, or mutates state.
"""

import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Ensure the scripts directory is on sys.path so sibling modules resolve.
_SCRIPTS_DIR = str(pathlib.Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _resolve_local_cognee_version() -> str:
    """Cognee version installed in the plugin's managed venv.

    The plugin installs cognee into ~/.cognee-plugin/venv at session start;
    probe that interpreter directly. Returns "Not installed" when the venv is
    absent and "Unknown" if the probe fails.
    """
    from _plugin_common import _VENV_PYTHON

    if not _VENV_PYTHON.exists():
        return "Not installed"
    try:
        probe = "import importlib.metadata as m; print(m.version('cognee'))"
        out = subprocess.run(
            [str(_VENV_PYTHON), "-c", probe],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "Unknown"


def _resolve_mode() -> str:
    """Return the resolved operating mode: Local, Local Managed, or Cloud.

    - No base_url configured → Local
    - base_url pointing to localhost / 127.0.0.1 / ::1 → Local Managed
    - Remote base_url → Cloud
    """
    import urllib.parse

    from config import load_config

    cfg = load_config()
    base_url = str(cfg.get("base_url") or "").strip()

    if not base_url:
        return "Local"

    hostname = urllib.parse.urlparse(base_url).hostname or ""
    if hostname in ("localhost", "127.0.0.1", "::1"):
        return "Local Managed"

    return "Cloud"


_DEFAULT_LOCAL_SERVICE_URL = "http://localhost:8011"


def _resolve_server_url() -> tuple:
    """Return (display_url, raw_url).

    Codex's _plugin_common does not expose _local_api_url_with_source,
    so we inline the same resolution logic here (env → default).
    In local mode the display value is "-".
    """
    raw_url = (
        os.environ.get("COGNEE_LOCAL_API_URL")
        or os.environ.get("COGNEE_BASE_URL")
        or ""
    ).strip() or _DEFAULT_LOCAL_SERVICE_URL

    mode = _resolve_mode()
    display = "-" if mode == "Local" else raw_url
    return display, raw_url


_SHARED_PLUGIN_ROOT = pathlib.Path.home() / ".cognee-plugin"
_API_KEY_CACHE = _SHARED_PLUGIN_ROOT / "api_key.json"


def _resolve_api_key_source() -> str:
    """Return a human-friendly label for where the API key came from.

    Codex's _plugin_common._api_key does not return the source, so we
    inline the same precedence logic here without modifying the module.
    """
    env_key = (os.environ.get("COGNEE_API_KEY") or "").strip()
    if env_key:
        return "ENV"

    # Check the single cached key file.
    try:
        if _API_KEY_CACHE.exists():
            data = json.loads(_API_KEY_CACHE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and (data.get("api_key") or "").strip():
                return "Config"
    except Exception:
        pass

    return "Default"


def _check_health(server_url: str, timeout: float = 5.0) -> dict:
    """Probe GET /health and return reachability + latency.

    Returns a dict with keys: reachable (bool), latency_ms (float|None),
    and raw_body (dict|None) for downstream consumers.
    """
    base = server_url.rstrip("/") if server_url else ""
    if not base:
        return {"reachable": False, "latency_ms": None, "raw_body": None}
    try:
        t0 = time.monotonic()
        with urllib.request.urlopen(f"{base}/health", timeout=timeout) as resp:
            latency = (time.monotonic() - t0) * 1000  # ms
            body_text = resp.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(body_text)
            except (json.JSONDecodeError, ValueError):
                body = None
            if resp.status == 200:
                return {"reachable": True, "latency_ms": round(latency, 1), "raw_body": body}
        return {"reachable": False, "latency_ms": None, "raw_body": None}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"reachable": False, "latency_ms": None, "raw_body": None}



def _resolve_server_version(health_body: dict | None) -> str:
    """Extract a server version from the health response, if present."""
    if isinstance(health_body, dict):
        version = health_body.get("version")
        if version and str(version).strip():
            return str(version).strip()
    return "Unknown"


def _resolve_circuit_breaker() -> str:
    """Return a human description of the circuit breaker state."""
    from _cognee_client import breaker_open

    is_open, retry = breaker_open()
    if is_open:
        return f"Open (retry in ~{retry}s)"
    return "Closed"


def _resolve_embedding() -> tuple[str, str]:
    """Embedding model + dimensions from the environment cognee reads.

    cognee resolves embeddings from EMBEDDING_MODEL / EMBEDDING_DIMENSIONS; no
    HTTP endpoint exposes them, so we surface what the local environment sets
    (the values that govern local mode). "Default" means unset — cognee falls
    back to its built-in default.
    """
    model = (os.environ.get("EMBEDDING_MODEL") or "").strip() or "Default"
    dims = (os.environ.get("EMBEDDING_DIMENSIONS") or "").strip() or "Default"
    return model, dims


def collect_report() -> dict:
    """Gather all diagnostic fields into an ordered dict."""
    mode = _resolve_mode()
    display_url, raw_url = _resolve_server_url()
    api_key_source = _resolve_api_key_source()
    health = _check_health(raw_url)
    cognee_server = _resolve_server_version(health["raw_body"])
    cognee_local = _resolve_local_cognee_version()
    circuit_breaker = _resolve_circuit_breaker()
    embedding_model, embedding_dimensions = _resolve_embedding()

    return {
        "mode": mode,
        "server_url": display_url if display_url != "-" else None,
        "api_key_source": api_key_source,
        "reachable": health["reachable"],
        "latency_ms": health["latency_ms"],
        "cognee_local": cognee_local,
        "cognee_server": cognee_server,
        "embedding_model": embedding_model,
        "embedding_dimensions": embedding_dimensions,
        "circuit_breaker": circuit_breaker,
    }


_DISPLAY_ORDER = [
    ("Mode", "mode"),
    ("Server URL", "server_url"),
    ("API Key Source", "api_key_source"),
    ("Reachable", "reachable"),
    ("Latency", "latency_ms"),
    ("Cognee (local)", "cognee_local"),
    ("Cognee (server)", "cognee_server"),
    ("Embedding Model", "embedding_model"),
    ("Embedding Dims", "embedding_dimensions"),
    ("Circuit Breaker", "circuit_breaker"),
]


def _format_value(key: str, value) -> str:
    """Format a single report value for human display."""
    if key == "server_url":
        return str(value) if value else "-"
    if key == "reachable":
        return "Yes" if value else "No"
    if key == "latency_ms":
        if value is None:
            return "N/A"
        return f"{value} ms"
    if value is None:
        return "N/A"
    return str(value)


def format_human(report: dict) -> str:
    """Render the report as a human-readable table."""
    lines = ["", "Cognee Doctor", ""]
    for label, key in _DISPLAY_ORDER:
        value = _format_value(key, report.get(key))
        lines.append(f"{label + ':':<21}{value}")
    lines.append("")
    return "\n".join(lines)


def format_json(report: dict) -> str:
    """Render the report as pretty-printed JSON."""
    return json.dumps(report, indent=2)



def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    use_json = "--json" in args

    report = collect_report()

    if use_json:
        print(format_json(report))
    else:
        print(format_human(report))


if __name__ == "__main__":
    main()
