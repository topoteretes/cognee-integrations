"""Configuration for the Band memory adapter.

Reads the same one-time setup file as the Claude Code and Codex plugins
(``~/.cognee/.env``), with identical precedence:

  real exported env vars  >  ~/.cognee/.env  >  defaults

The POC is a pure thin client: it requires a reachable Cognee server
(``COGNEE_BASE_URL``) and does not bootstrap a local runtime. Loading is
stdlib-only and must never raise — a malformed env file is ignored, not fatal.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

_COGNEE_HOME = Path.home() / ".cognee"
_DEFAULT_ENV_FILE = _COGNEE_HOME / ".env"

DEFAULT_BASE_URL = "http://localhost:8011"
DEFAULT_DATASET = "agent_sessions"

# Vars that could hijack process behavior if injected from a config file.
_DENYLIST_EXACT = {"PATH", "HOME", "PYTHONPATH", "PYTHONHOME", "SHELL", "USER"}
_DENYLIST_PREFIXES = ("LD_", "DYLD_")


def env_file_path() -> Path:
    """The env file location (``COGNEE_ENV_FILE`` overrides the default)."""
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
    """Inject env-file values into os.environ (setdefault). Never raises."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        path = env_file_path()
        if not path.is_file():
            return
        for key, value in parse_env_file(path).items():
            if _blocked(key):
                continue
            os.environ.setdefault(key, value)
    except Exception:
        pass


@dataclass
class CogneeSettings:
    """Resolved connection + memory settings for one adapter instance."""

    base_url: str = ""
    api_key: str = ""
    dataset: str = DEFAULT_DATASET
    top_k: int = 5
    recall_timeout: float = 20.0
    store_timeout: float = 30.0
    improve_timeout: float = 180.0
    # Prefix for Cognee session ids derived from Band room ids.
    session_prefix: str = "band"
    extra: dict = field(default_factory=dict)

    @classmethod
    def resolve(cls, **overrides) -> "CogneeSettings":
        """Build settings from env (after loading ~/.cognee/.env), then overrides."""
        load_env_file()
        settings = cls(
            base_url=os.environ.get("COGNEE_BASE_URL", "").strip() or DEFAULT_BASE_URL,
            api_key=os.environ.get("COGNEE_API_KEY", "").strip(),
            dataset=os.environ.get("COGNEE_PLUGIN_DATASET", "").strip() or DEFAULT_DATASET,
        )
        top_k = os.environ.get("COGNEE_RECALL_TOP_K", "").strip()
        if top_k.isdigit() and int(top_k) > 0:
            settings.top_k = int(top_k)
        for key, value in overrides.items():
            if value is not None and hasattr(settings, key):
                setattr(settings, key, value)
        return settings

    def session_id_for_room(self, room_id: str) -> str:
        return f"{self.session_prefix}-{room_id}"
