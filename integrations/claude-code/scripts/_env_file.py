"""One-time plugin configuration via ~/.cognee/.env.

Users historically had to `export COGNEE_BASE_URL=...`, `export LLM_API_KEY=...`
in every shell. This module makes that a one-time step: values placed in
``~/.cognee/.env`` (the durable, venv-rebuild-safe cognee home shared by the
Claude Code and Codex plugins) are injected into ``os.environ`` at process
start with **setdefault** semantics, so:

  real exported env vars  >  ~/.cognee/.env  >  config.json  >  defaults

Every existing ``os.environ.get(...)`` call site — and every child process the
hooks spawn (the local cognee server, idle/exit watchers) — picks the values up
unchanged.

Deliberately stdlib-only: hook scripts must run outside the plugin venv on the
cloud path, so python-dotenv is not an option. The parser accepts a leading
``export `` so users can paste their existing shell export lines verbatim.

Loading must never break a hook: any parse or IO problem results in the file
being (partially) ignored, never an exception.
"""

import os
import stat
import sys
from pathlib import Path

_COGNEE_HOME = Path.home() / ".cognee"
_DEFAULT_ENV_FILE = _COGNEE_HOME / ".env"

# Vars that could hijack process behavior if injected from a config file.
# Everything else (COGNEE_*, LLM_*, EMBEDDING_*, provider keys, ...) is allowed
# so the values also flow through to the spawned local cognee server.
_DENYLIST_EXACT = {"PATH", "HOME", "PYTHONPATH", "PYTHONHOME", "SHELL", "USER"}
_DENYLIST_PREFIXES = ("LD_", "DYLD_")

_TEMPLATE = """\
# Cognee plugin configuration — shared by the Claude Code and Codex plugins.
# Values here are loaded at session start and act like shell exports, except
# you only set them once. A real `export` in your shell still wins over this
# file. Lines starting with `#` are comments; a leading `export ` is allowed.

## Cloud / remote mode — point the plugins at a Cognee instance:
# COGNEE_BASE_URL="https://your-instance.cognee.ai"
# COGNEE_API_KEY="ck_..."

## Local mode (default when COGNEE_BASE_URL is unset) — the plugin runs a
## local Cognee API; only an LLM key is required:
# LLM_API_KEY="sk-..."
# LLM_MODEL="openai/gpt-4o-mini"

## Optional:
# COGNEE_PLUGIN_DATASET="agent_sessions"
"""


def env_file_path() -> Path:
    """The env file location (COGNEE_ENV_FILE overrides the default)."""
    override = os.environ.get("COGNEE_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return _DEFAULT_ENV_FILE


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _blocked(key: str) -> bool:
    return key in _DENYLIST_EXACT or key.startswith(_DENYLIST_PREFIXES)


def parse_env_file(path: Path) -> dict:
    """Parse a dotenv-style file into a dict. Malformed lines are skipped.

    Supported: ``KEY=VALUE``, blank lines, ``#`` comments, an optional leading
    ``export ``, and single/double quotes around the value. No interpolation,
    no multi-line values.
    """
    result: dict[str, str] = {}
    try:
        # utf-8-sig strips a UTF-8 BOM when present (Windows PowerShell 5.1
        # writes one) and is a no-op otherwise. A PS5 `>` redirect produces
        # UTF-16 instead — retry with that before giving up.
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-16")
    except Exception:
        return result

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not all(c.isalnum() or c == "_" for c in key):
            continue
        result[key] = _unquote(value)
    return result


_loaded = False


def load_env_file() -> None:
    """Inject env-file values into os.environ (setdefault). Never raises.

    Idempotent per process: repeated calls (this module is imported from
    several entry-point modules) parse the file at most once.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True

    try:
        path = env_file_path()
        if not path.is_file():
            return
        _tighten_permissions(path)
        for key, value in parse_env_file(path).items():
            if _blocked(key):
                continue
            os.environ.setdefault(key, value)
    except Exception:
        pass


def _tighten_permissions(path: Path) -> None:
    """Best-effort chmod 0600: the file may hold API keys."""
    if os.name == "nt":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.chmod(0o600)
    except Exception:
        pass


def ensure_env_file_template() -> bool:
    """Write a commented template to ~/.cognee/.env if no file exists yet.

    Returns True when the template was created. Called from SessionStart so
    users find a documented file to fill in instead of an empty folder. Only
    the default location gets a template — an explicit COGNEE_ENV_FILE
    pointing elsewhere is the user's own file to manage.
    """
    if os.environ.get("COGNEE_ENV_FILE", "").strip():
        return False
    try:
        if _DEFAULT_ENV_FILE.exists():
            return False
        _COGNEE_HOME.mkdir(parents=True, exist_ok=True)
        _DEFAULT_ENV_FILE.write_text(_TEMPLATE, encoding="utf-8")
        if os.name != "nt":
            _DEFAULT_ENV_FILE.chmod(0o600)
        return True
    except Exception:
        return False


def env_file_status() -> dict:
    """Diagnostics for doctor: existence, perms, and key *names* (no values)."""
    path = env_file_path()
    info: dict = {"path": str(path), "exists": path.is_file()}
    if not info["exists"]:
        return info
    try:
        info["mode"] = oct(stat.S_IMODE(path.stat().st_mode))
    except Exception:
        pass
    values = parse_env_file(path)
    info["keys"] = sorted(values)
    info["blocked"] = sorted(k for k in values if _blocked(k))
    # A shell export (or another source) that set a different value wins over
    # the file; surface those so mode-selection surprises are debuggable.
    info["overridden"] = sorted(
        k for k in values if not _blocked(k) and os.environ.get(k, values[k]) != values[k]
    )
    return info


if __name__ == "__main__":
    import json

    load_env_file()
    json.dump(env_file_status(), sys.stdout, indent=2)
    print()
