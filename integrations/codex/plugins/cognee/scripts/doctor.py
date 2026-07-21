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
import sys
import time
import urllib.error
import urllib.request

# Ensure the scripts directory is on sys.path so sibling modules resolve.
_SCRIPTS_DIR = str(pathlib.Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


_INVENTORY_SLUG = "codex"

def _resolve_plugin_version() -> str:
    """Read the plugin version from inventory.yml.

    Walks up from the scripts directory looking for inventory.yml,
    then extracts the current_version for the codex slug.
    Falls back to "Unknown" if the file is missing or unparseable.
    """
    search = pathlib.Path(__file__).resolve().parent
    for _ in range(10):
        candidate = search / "inventory.yml"
        if candidate.exists():
            return _parse_inventory_version(candidate, _INVENTORY_SLUG)
        if search.parent == search:
            break
        search = search.parent
    return "Unknown"


def _parse_inventory_version(path: pathlib.Path, slug: str) -> str:
    """Extract current_version for *slug* from a YAML inventory file.

    Uses a simple line-scanner instead of importing PyYAML (which may
    not be installed in the host interpreter).
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        found_slug = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- slug:"):
                found_slug = stripped.split(":", 1)[1].strip().strip('"').strip("'") == slug
            elif found_slug and stripped.startswith("current_version:"):
                raw = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                return raw or "Unknown"
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



def collect_report() -> dict:
    """Gather all diagnostic fields into an ordered dict."""
    mode = _resolve_mode()
    display_url, raw_url = _resolve_server_url()
    api_key_source = _resolve_api_key_source()
    health = _check_health(raw_url)
    server_version = _resolve_server_version(health["raw_body"])
    plugin_version = _resolve_plugin_version()
    circuit_breaker = _resolve_circuit_breaker()

    return {
        "mode": mode,
        "server_url": display_url if display_url != "-" else None,
        "api_key_source": api_key_source,
        "reachable": health["reachable"],
        "latency_ms": health["latency_ms"],
        "plugin_version": plugin_version,
        "server_version": server_version,
        "embedding_model": "Unknown",
        "embedding_dimensions": "Unknown",
        "circuit_breaker": circuit_breaker,
    }


_DISPLAY_ORDER = [
    ("Mode", "mode"),
    ("Server URL", "server_url"),
    ("API Key Source", "api_key_source"),
    ("Reachable", "reachable"),
    ("Latency", "latency_ms"),
    ("Plugin Version", "plugin_version"),
    ("Server Version", "server_version"),
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
