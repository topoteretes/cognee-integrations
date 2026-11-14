#!/usr/bin/env python3
"""Cognee Doctor — unified diagnostics for the Claude Code plugin.

Read-only command that aggregates configuration, connectivity, and
circuit-breaker state into a single diagnostic report.

Usage:
    python doctor.py           # human-readable table
    python doctor.py --json    # machine-readable JSON

Never modifies configuration, initialises databases, registers
resources, writes files, or mutates state.
"""

from __future__ import annotations

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

# The settings layer, first: it applies the plugin options (or, from a shell,
# the saved URL option) and the env file's tuning names before anything below
# reads the environment.
import config  # noqa: E402,F401


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

    - No base_url configured → Local (or Cloud when the backend switch forces
      cloud — the mode is pinned even though there is nothing to connect to)
    - base_url pointing to localhost / 127.0.0.1 / ::1 → Local Managed
    - Remote base_url → Cloud
    """
    import urllib.parse

    from config import load_config

    cfg = load_config()
    base_url = str(cfg.get("base_url") or "").strip()

    if not base_url:
        return "Cloud" if cfg.get("_forced_backend") == "cloud" else "Local"

    hostname = urllib.parse.urlparse(base_url).hostname or ""
    if hostname in ("localhost", "127.0.0.1", "::1"):
        return "Local Managed"

    return "Cloud"


def _mode_annotation() -> str:
    """Suffix for the mode row when the backend switch forced the decision."""
    from _env_file import forced_backend_with_source
    from config import load_config

    forced, var = forced_backend_with_source()
    if not forced:
        return ""
    note = f" — forced by {var}={forced}"
    if forced == "cloud" and not str(load_config().get("base_url") or "").strip():
        note += " (the Cloud URL option is empty — nothing to connect to)"
    return note


def _resolve_server_url() -> tuple:
    """Return (display_url, raw_url).

    In local mode the display value is "-" (no remote server), but the
    raw_url is still resolved so the health-check can probe localhost.
    Forced cloud with no URL configured has nothing to probe at all.
    """
    from _plugin_common import _local_api_url_with_source
    from config import load_config

    mode = _resolve_mode()
    if mode == "Cloud" and not str(load_config().get("base_url") or "").strip():
        return "-", ""
    url, _source = _local_api_url_with_source()
    display = "-" if mode == "Local" else url
    return display, url


_KEY_SOURCE_LABELS = {
    "env_api_key": "Plugin option",
    "plugin_option": "Plugin option",
    "local_owner_key": "Local server",
    "missing": "Not set",
}
#: Shown for a setting only the plugin options hold when no session has
#: recorded it yet: doctor runs from a shell, which Claude Code gives no options.
NOT_VISIBLE = (
    "set in the plugin options (not visible from a shell); start a session to see the current state"
)


def _has_options() -> bool:
    """True when this process got the plugin options (run from a hook)."""
    from _env_file import OPTION_PREFIX, is_hook_process

    return is_hook_process() or any(name.startswith(OPTION_PREFIX) for name in os.environ)


def _launch_facts() -> dict:
    """What the newest session start recorded for this project (or any), or {}."""
    from _plugin_common import latest_launch_facts

    cwd = os.environ.get("CLAUDE_CWD", "") or os.getcwd()
    try:
        return latest_launch_facts(cwd)
    except Exception:
        return {}


def _as_of(facts: dict) -> str:
    stamp = float(facts.get("recorded_at") or 0)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else "unknown time"
    return f"(as of session start, {when})"


def _resolve_api_key_source() -> str:
    """Return a human-friendly label for where the API key came from.

    Run from a shell, doctor has no plugin options, so it reports what the
    newest session start decided rather than recomputing it without them.
    """
    if _has_options():
        from _plugin_common import _api_key_with_source

        _key, source = _api_key_with_source()
        return _KEY_SOURCE_LABELS.get(source, source)
    facts = _launch_facts()
    source = str(facts.get("key_source") or "")
    if not source:
        return NOT_VISIBLE
    return f"{_KEY_SOURCE_LABELS.get(source, source)} {_as_of(facts)}"


def _resolve_memory_sharing() -> str:
    """How this plugin's memory relates to the user's other agents.

    The Claude Code plugin runs as the user's own key (no plugin identity), so
    it sees every dataset the user can; there is no shared agent role.
    """
    return "principal (no plugin identity)"


def _check_health(server_url: str, timeout: float = 5.0) -> dict:
    """Probe GET /health and return reachability + latency.

    Returns a dict with keys: reachable (bool), latency_ms (float|None),
    and raw_body (dict|None) for downstream consumers.
    """
    base = server_url.rstrip("/") if server_url else ""
    if not base:
        return {"reachable": False, "latency_ms": None, "raw_body": None}
    from _plugin_common import _https_context

    try:
        t0 = time.monotonic()
        with urllib.request.urlopen(
            f"{base}/health", timeout=timeout, context=_https_context()
        ) as resp:
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


_RUNTIME_PYTHON_MIN = (3, 10)
_RUNTIME_PYTHON_MAX = (3, 14)
_HOST_PYTHON_CANDIDATES = (
    "python3.14",
    "python3.13",
    "python3.12",
    "python3.11",
    "python3.10",
    "python3",
    "python",
)


def _python_version_of(python: str) -> tuple:
    """``(major, minor, micro)`` of an interpreter on disk, or ``()``."""
    try:
        out = subprocess.run(
            [python, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return tuple(int(part) for part in out.stdout.strip().split("."))
    except Exception:
        pass
    return ()


def _resolve_runtime_python() -> str:
    """The Python the local Cognee server runs on — or why there is none.

    Cognee requires 3.10-3.14 and the plugin builds its runtime venv only from an
    interpreter already installed (it downloads none), so this is the first thing
    to check when local mode will not start. Reports the venv's interpreter once
    built; before that, the SessionStart refusal marker if one was recorded,
    otherwise the newest suitable ``python3.x`` on PATH (the one the next session
    start will use). Read-only: probes versions, builds nothing.
    """
    import shutil

    from _plugin_common import _SHARED_PLUGIN_ROOT, _VENV_PYTHON

    if _resolve_mode() == "Cloud":
        return "Not needed (remote server)"
    if _VENV_PYTHON.exists():
        version = _python_version_of(str(_VENV_PYTHON))
        label = ".".join(str(part) for part in version) if version else "unknown version"
        return f"{label} (plugin venv)"
    need = "Cognee needs Python {}.{}-{}.{}".format(*_RUNTIME_PYTHON_MIN, *_RUNTIME_PYTHON_MAX)
    marker = _SHARED_PLUGIN_ROOT / "host-python-unsupported.json"
    if marker.exists():
        return f"MISSING - {need}; none found at last session start (install one, then restart)"
    seen = set()
    for name in _HOST_PYTHON_CANDIDATES + (sys.executable,):
        found = shutil.which(name) if os.sep not in name else name
        if not found or found in seen:
            continue
        seen.add(found)
        version = _python_version_of(found)
        if version and _RUNTIME_PYTHON_MIN <= version[:2] <= _RUNTIME_PYTHON_MAX:
            return "{}.{}.{} ({}, venv not built yet)".format(*version[:3], found)
    return f"MISSING - {need}; none found on PATH (install one, then start a session)"


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


def _stored_login_note(reason: str) -> str:
    try:
        from _observer import STORED_LOGIN_ONLY_MESSAGE, STORED_LOGIN_ONLY_REASON
    except ImportError:
        return ""
    return STORED_LOGIN_ONLY_MESSAGE if reason == STORED_LOGIN_ONLY_REASON else ""


def _observer_health_reason() -> str:
    """The reason the observer health check last recorded (llm-state marker), or ""."""
    try:
        from _plugin_common import read_llm_state

        return str(read_llm_state().get("reason") or "")
    except Exception:
        return ""


def _resolve_llm() -> str:
    """Where the local server's LLM calls go: a configured provider key, or the
    Claude observer (Claude Code's own subscription via ``claude -p``).

    Local mode only — a remote server owns its own LLM. The LLM settings are
    plugin options, which a shell does not have, so without them this reports
    what session start recorded; the shim's ``/health`` is still asked live.
    Read-only: it never starts the shim or touches the environment.
    """
    mode = _resolve_mode()
    if mode == "Cloud":
        return "Remote server (not applicable)"
    health_note = _stored_login_note(_observer_health_reason())
    result = _computed_llm() if _has_options() else _recorded_llm()
    if health_note and health_note not in result:
        result += f" {health_note}"
    return result


def _recorded_llm() -> str:
    from _observer import observer_alive, port

    facts = _launch_facts()
    if not facts:
        return NOT_VISIBLE
    observer = facts.get("observer") if isinstance(facts.get("observer"), dict) else {}
    reason = str(observer.get("reason") or "")
    as_of = _as_of(facts)
    if observer.get("active"):
        shim = "shim running" if observer_alive(port()) else "shim not running"
        return (
            f"Claude Code observer (`claude -p`, model {observer.get('model') or 'default'}, "
            f"{shim}) {as_of}"
        )
    note = _stored_login_note(reason)
    if note:
        return f"None — {note} {as_of}"
    if facts.get("llm_key_option_set") or facts.get("llm_provider"):
        provider = str(facts.get("llm_provider") or "") or "openai (default)"
        model = str(facts.get("llm_model") or "") or "Default"
        return f"Configured provider ({provider}, model {model}) {as_of}"
    if reason == "claude_cli_missing":
        return f"None — no LLM API key option and no `claude` executable for the observer {as_of}"
    if reason == "disabled":
        return (
            f"None — no LLM API key option; observer disabled (COGNEE_LLM_OBSERVER=false) {as_of}"
        )
    return f"None — no LLM API key option set {as_of}"


def _computed_llm() -> str:
    """The observer decision made here, with this process's plugin options."""
    from _observer import resolve_observer
    from config import load_config

    try:
        decision = resolve_observer(load_config())
    except Exception as exc:
        return f"Unknown ({str(exc)[:80]})"
    if decision.get("active"):
        from _observer import SPEND_WARNING, embedding_warning, observer_alive

        shim = "shim running" if observer_alive(decision.get("port")) else "shim not running"
        return (
            f"Claude Code observer (`claude -p`, model {decision.get('model')}, "
            f"{decision.get('endpoint')}, {shim}). {SPEND_WARNING} {embedding_warning()}"
            + (f" Warning: {decision['model_warning']}" if decision.get("model_warning") else "")
        )
    if decision.get("error"):
        return f"None — {decision['error']}"
    reason = str(decision.get("reason") or "")
    note = _stored_login_note(reason)
    if note:
        return f"None — {note}"
    if reason in ("llm_key_configured", "llm_provider_configured"):
        provider = (os.environ.get("LLM_PROVIDER") or "").strip() or "openai (default)"
        model = (os.environ.get("LLM_MODEL") or "").strip() or "Default"
        return f"Configured provider ({provider}, model {model})"
    if reason == "claude_cli_missing":
        return "None — no LLM API key option and no `claude` executable for the observer"
    if reason == "disabled":
        return "None — no LLM API key option; observer disabled (COGNEE_LLM_OBSERVER=false)"
    return "None — no LLM API key option set"


def _resolve_env_file() -> str:
    """Tuning file (~/.cognee/.env): presence, the names it sets, what is ignored.

    Values are never shown — only which settings the file defines, which of
    them are shadowed by a real shell export, and which it holds that the
    plugin no longer reads (credentials, the Cloud URL, LLM settings).
    """
    from _env_file import env_file_status

    status = env_file_status()
    path = status.get("path", "")
    if not status.get("exists"):
        return f"Not found ({path})"
    keys = status.get("keys") or []
    desc = f"{path} ({len(keys)} setting{'s' if len(keys) != 1 else ''}: {', '.join(keys)})"
    overridden = status.get("overridden") or []
    if overridden:
        desc += f" — overridden by shell env: {', '.join(overridden)}"
    refused = status.get("refused") or []
    if refused:
        desc += f" — {', '.join(refused)} ignored (set in the plugin options)"
    ignored = status.get("ignored") or []
    if ignored:
        desc += f" — {', '.join(ignored)} ignored (not a plugin setting)"
    return desc


def collect_report() -> dict:
    """Gather all diagnostic fields into an ordered dict."""
    from _auto_memory import state as auto_memory_state
    from config import load_config

    mode = _resolve_mode()
    display_url, raw_url = _resolve_server_url()
    api_key_source = _resolve_api_key_source()
    memory_sharing = _resolve_memory_sharing()
    health = _check_health(raw_url)
    cognee_server = _resolve_server_version(health["raw_body"])
    cognee_local = _resolve_local_cognee_version()
    runtime_python = _resolve_runtime_python()
    circuit_breaker = _resolve_circuit_breaker()
    embedding_model, embedding_dimensions = _resolve_embedding()

    report = {
        "mode": mode + _mode_annotation(),
        "env_file": _resolve_env_file(),
        "server_url": display_url if display_url != "-" else None,
        "api_key_source": api_key_source,
        "memory_sharing": memory_sharing,
        "reachable": health["reachable"],
        "latency_ms": health["latency_ms"],
        "runtime_python": runtime_python,
        "cognee_local": cognee_local,
        "cognee_server": cognee_server,
        "llm": _resolve_llm(),
        "embedding_model": embedding_model,
        "embedding_dimensions": embedding_dimensions,
        "circuit_breaker": circuit_breaker,
        "auto_memory": auto_memory_state(),
    }
    from config import coding_memory_enabled, get_dataset

    if coding_memory_enabled():
        # Shown only while COGNEE_CODING_MEMORY is on (SDK-1336).
        try:
            from _coding_memory import doctor_line

            report["coding_memory"] = doctor_line(get_dataset(load_config()))
        except Exception as exc:
            report["coding_memory"] = "check failed: " + str(exc)[:120]
    return report


_DISPLAY_ORDER = [
    ("Mode", "mode"),
    ("Env File", "env_file"),
    ("Server URL", "server_url"),
    ("API Key Source", "api_key_source"),
    ("Memory Sharing", "memory_sharing"),
    ("Reachable", "reachable"),
    ("Latency", "latency_ms"),
    ("Runtime Python", "runtime_python"),
    ("Cognee (local)", "cognee_local"),
    ("Cognee (server)", "cognee_server"),
    ("LLM", "llm"),
    ("Embedding Model", "embedding_model"),
    ("Embedding Dims", "embedding_dimensions"),
    ("Circuit Breaker", "circuit_breaker"),
    ("Claude Auto Memory", "auto_memory"),
    ("Coding Memory", "coding_memory"),
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
