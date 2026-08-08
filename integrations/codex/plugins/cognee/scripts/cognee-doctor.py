#!/usr/bin/env python3
"""cognee-plugin doctor: print a single-page health snapshot.

Usage:
  python3 cognee-doctor.py

Reads no stdin, writes only to stdout. Exit code 0 always.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

_PLUGIN_LABEL = "codex"   # change to "claude-code" in the claude-code copy

try:
    import _plugin_common as _pc
    _SHARED_ROOT  = _pc._SHARED_PLUGIN_ROOT
    _VENV_PYTHON  = _pc._VENV_PYTHON
except Exception:
    _SHARED_ROOT  = Path.home() / ".cognee-plugin"
    _VENV_PYTHON  = _SHARED_ROOT / "venv" / "bin" / "python"

_BREAKER_PATH = _SHARED_ROOT / "recall-breaker.json"


def _load_config() -> dict:
    try:
        from config import load_config
        return load_config()
    except Exception:
        return {}


def _resolve_mode_url_key(cfg: dict) -> tuple:
    try:
        from _plugin_common import _api_key_with_source, _local_api_url
        base_url = _local_api_url()
        mode = "cloud" if base_url and "localhost" not in base_url else "local"
        _, key_source = _api_key_with_source(base_url)
    except Exception:
        base_url = cfg.get("base_url", "")
        mode = "cloud" if base_url else "local"
        key_source = "env_api_key" if os.environ.get("COGNEE_API_KEY") else "missing"
    return mode, base_url or "(none — local mode)", key_source


def _check_server(base_url: str, timeout: float = 5.0) -> tuple:
    if not base_url or base_url == "(none — local mode)":
        return False, 0.0, "n/a"
    url = base_url.rstrip("/") + "/health"
    try:
        t0 = time.perf_counter()
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            latency = (time.perf_counter() - t0) * 1000
            body = resp.read().decode("utf-8", errors="replace")
        try:
            version = json.loads(body).get("version") or "n/a"
        except Exception:
            version = "n/a"
        return True, latency, str(version)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return False, 0.0, str(exc)


def _venv_cognee_version() -> str:
    if not Path(str(_VENV_PYTHON)).exists():
        return "n/a  (venv not found)"
    try:
        out = subprocess.run(
            [str(_VENV_PYTHON), "-c",
             "import importlib.metadata as m; print(m.version('cognee'))"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or "n/a"
    except Exception:
        return "n/a"


def _embedding_info() -> tuple:
    return (
        os.environ.get("EMBEDDING_MODEL", "") or "n/a",
        os.environ.get("EMBEDDING_DIMENSIONS", "") or "n/a",
        os.environ.get("VECTOR_DB_PROVIDER", "") or "n/a",
    )


def _breaker_state() -> tuple:
    try:
        raw = json.loads(_BREAKER_PATH.read_text(encoding="utf-8"))
        failures = int(raw.get("failures", 0) or 0)
        until    = float(raw.get("cooldown_until", 0) or 0)
        if time.time() < until:
            retry = int(until - time.time())
            return f"OPEN — retry in {retry}s  ({failures} failures)", True
        return f"closed  ({failures} failures)", False
    except FileNotFoundError:
        return "closed  (0 failures — no state file yet)", False
    except Exception as exc:
        return f"unknown  ({exc})", False


def main() -> None:
    cfg = _load_config()
    mode, base_url, key_source = _resolve_mode_url_key(cfg)
    dataset   = cfg.get("dataset") or "agent_sessions"
    llm_model = cfg.get("llm_model") or "(not set)"
    backend   = cfg.get("backend") or "auto"

    reachable, latency_ms, server_version = _check_server(base_url)
    venv_version = _venv_cognee_version()
    embed_model, embed_dims, vector_db = _embedding_info()
    breaker_label, breaker_open = _breaker_state()

    lines = [
        f"=== cognee-plugin doctor ({_PLUGIN_LABEL}) ===", "",
        "[config]",
        f"  mode          : {mode}",
        f"  base_url      : {base_url}",
        f"  key_source    : {key_source}",
        f"  dataset       : {dataset}",
        f"  llm_model     : {llm_model}",
        f"  backend       : {backend}",
        "",
        "[server]",
        f"  reachable     : yes  (latency {latency_ms:.0f} ms)" if reachable
            else f"  reachable     : no  ({server_version})",
        f"  server_version: {server_version if reachable else 'n/a'}",
        f"  venv_version  : {venv_version}",
        "",
        "[embedding]",
        f"  model         : {embed_model}",
        f"  dimensions    : {embed_dims}",
        f"  vector_db     : {vector_db}",
        "",
        "[circuit breaker]",
        f"  state         : {breaker_label}",
        f"  breaker_file  : {_BREAKER_PATH}",
        "",
        "[summary]",
        f"  {'✓' if reachable else '✕'}  Server {'reachable' if reachable else 'UNREACHABLE'}",
        f"  {'✓' if key_source != 'missing' else '✕'}  Key "
            + (f"present ({key_source})" if key_source != "missing"
               else "MISSING — set COGNEE_API_KEY"),
    ]

    if breaker_open:
        lines.append("  ✕  Circuit breaker OPEN")

    sv = server_version if reachable else ""
    vv = venv_version if "n/a" not in venv_version else ""
    if sv and sv != "n/a" and vv:
        if sv == vv:
            lines.append(f"  ✓  Versions match (venv == server: {sv})")
        else:
            lines.append(f"  ✕  Version mismatch  venv={vv}  server={sv}")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
