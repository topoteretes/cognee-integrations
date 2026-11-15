"""Plugin settings: credentials from the plugin options, tuning from ~/.cognee/.env.

Every credential and connection setting comes from the plugin's options
(``userConfig`` in .claude-plugin/plugin.json). Claude Code keeps the sensitive
ones in its secure store and hands all of them to hook processes as
``CLAUDE_PLUGIN_OPTION_<KEY>``. A key is read from its option where it is used
(``cloud_key``, ``server_secret_env``) and is never copied into another
environment variable of a plugin process: the plugin reads no credential from
the environment except the options themselves. ``_apply_plugin_options`` maps
the non-secret settings onto the variables the local server reads
(``LLM_PROVIDER``, ``LLM_MODEL``, ``EMBEDDING_*``). A value for any of those
names in the shell, keys included, is removed, so a credential never reaches
the plugin except through Claude Code.

``~/.cognee/.env`` (the durable cognee home shared with the Codex plugin) still
supplies the plugin's own non-secret ``COGNEE_*`` tuning settings, injected into
``os.environ`` with **setdefault** semantics (a real export wins). Only the
names in ``_TUNING_NAMES`` are loaded. The Cloud URL and key, the default-user
login, every ``LLM_*`` / ``EMBEDDING_*`` setting and any name that looks like a
credential (``_refused``) are never loaded: the parser drops their values and
keeps only their names, so doctor and the session-start upgrade notice can say
they are ignored.

Processes Claude Code gives no options (the status line, Bash-run skills) read
the non-secret URL option where Claude Code saves it: ``pluginConfigs`` in the
user settings. They have no key.

Deliberately stdlib-only: hook scripts must run outside the plugin venv on the
cloud path, so python-dotenv is not an option. The parser accepts a leading
``export `` so users can paste their existing shell export lines verbatim.

One export flips a single terminal: ``COGNEE_BACKEND=local`` (or ``=cloud``),
with the plugin-specific variable (``_PLUGIN_BACKEND_VAR``) beating the shared
name. A forced-local switch is applied to ``os.environ`` itself (see
``_apply_backend_switch``) so the HTTP hot paths and spawned children — which
read ``COGNEE_BASE_URL`` from the environment directly, not via
``config.load_config`` — see the same mode.

Loading must never break a hook: any parse or IO problem results in the file
being (partially) ignored, never an exception.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

from _net import is_loopback_host, is_loopback_url  # noqa: F401  (re-exported)

_COGNEE_HOME = Path.home() / ".cognee"
_DEFAULT_ENV_FILE = _COGNEE_HOME / ".env"

# The plugin options the local server is configured from, by their userConfig
# name, and the name the server reads each one by. The Cloud key option
# (``cognee_api_key``) has no entry: it is read where a request needs it
# (``cloud_key``) and never becomes a variable. The sensitive ones are kept by
# Claude Code in its secure store.
OPTION_PREFIX = "CLAUDE_PLUGIN_OPTION_"
OPTION_VARS = {
    "llm_api_key": "LLM_API_KEY",
    "llm_provider": "LLM_PROVIDER",
    "llm_model": "LLM_MODEL",
    "embedding_provider": "EMBEDDING_PROVIDER",
    "embedding_model": "EMBEDDING_MODEL",
    "embedding_dimensions": "EMBEDDING_DIMENSIONS",
    "embedding_api_key": "EMBEDDING_API_KEY",
}
SENSITIVE_OPTIONS = ("cognee_api_key", "llm_api_key", "embedding_api_key")
URL_OPTION = "cognee_base_url"
LLM_OPTIONS = tuple(name for name in OPTION_VARS if name.startswith(("llm_", "embedding_")))

# Never loaded from the file, and taken from the options instead of the shell.
_REFUSED_EXACT = frozenset({"COGNEE_BASE_URL", "COGNEE_API_KEY", "COGNEE_PRINCIPAL_API_KEY"})
_REFUSED_PREFIXES = ("COGNEE_USER_", "LLM_", "EMBEDDING_")
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH")
# Dropped from the environment of every plugin process, whatever put them
# there: the Cloud, LLM and embedding keys (each read from its option, never
# from a variable) and the login the plugin used to take from the environment
# (always the default user now, config.py).
_SHELL_DROPPED = (
    "COGNEE_API_KEY",
    "COGNEE_PRINCIPAL_API_KEY",
    "LLM_API_KEY",
    "EMBEDDING_API_KEY",
    "COGNEE_USER_EMAIL",
    "COGNEE_USER_PASSWORD",
)
# Where the plugin's local server keeps its stores: always the built-in
# location under ~/.cognee (set by ``_plugin_common.apply_cognee_env``), never
# a value from the shell or the file.
_STORAGE_DIR_VARS = ("SYSTEM_ROOT_DIRECTORY", "DATA_ROOT_DIRECTORY")
_STORAGE_DIR_DEFAULTS = {
    "SYSTEM_ROOT_DIRECTORY": str(_COGNEE_HOME / "system"),
    "DATA_ROOT_DIRECTORY": str(_COGNEE_HOME / "data"),
}
# Storage settings the plugin's server no longer takes from the shell or the
# file (names only): named once in the session-start upgrade notice.
_STORAGE_NAMES = frozenset(_STORAGE_DIR_VARS)
_STORAGE_PREFIXES = ("DB_", "GRAPH_DATABASE_", "VECTOR_DB_")
# Storage names this process found in the shell it was started from.
_shell_storage_names: list = []
# The same names (never values), carried in the environment across the re-exec
# into the plugin venv and to children: the new process sees only the
# defaults, so without this the notice would never fire.
_SHELL_STORAGE_IGNORED_VAR = "COGNEE_PLUGIN_SHELL_STORAGE_IGNORED"
# Honoured, from the file or the shell, only when it names this machine.
_LOCAL_URL_VAR = "COGNEE_LOCAL_API_URL"
# Set once the first plugin process has replaced the shell's LLM_* /
# EMBEDDING_* values with the options. A child inherits what its parent
# applied (the Claude observer's provider, endpoint and token included) and
# must keep it rather than strip it again.
_LLM_ENV_APPLIED = "COGNEE_PLUGIN_LLM_ENV_APPLIED"

# The plugin's own non-secret tuning settings: the only names ~/.cognee/.env
# may set. Anything else in the file is ignored.
_TUNING_NAMES = frozenset(
    {
        "COGNEE_AGENT_NAME",
        "COGNEE_AUTO_IMPROVE_EVERY",
        "COGNEE_BACKEND",
        "COGNEE_BREAKER_COOLDOWN",
        "COGNEE_BREAKER_THRESHOLD",
        "COGNEE_BREAKER_WINDOW",
        "COGNEE_CAPTURE",
        "COGNEE_CAPTURE_DENY_PATHS",
        "COGNEE_CAPTURE_DENY_TOOLS",
        "COGNEE_CAPTURE_REDACT",
        "COGNEE_CAPTURE_REDACT_PATTERNS",
        "COGNEE_CAPTURE_TOOLS",
        "COGNEE_CLAUDE_BACKEND",
        "COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE",
        "COGNEE_CODE_AUTOINDEX",
        "COGNEE_CODING_MEMORY",
        "COGNEE_PLUGIN_DISABLED",
        "COGNEE_COGNIFY_POLL_INTERVAL",
        "COGNEE_CONN_REPROBE_INTERVAL",
        "COGNEE_DRAIN_BUDGET",
        "COGNEE_DRAIN_BUDGET_FINAL",
        "COGNEE_FILE_CONTEXT",
        "COGNEE_FILE_CONTEXT_BUDGET",
        "COGNEE_FILE_CONTEXT_MAX_SYMBOLS",
        "COGNEE_FILE_CONTEXT_SCOPES",
        "COGNEE_FILE_CONTEXT_TTL",
        "COGNEE_IDLE_DISABLED",
        "COGNEE_IDLE_POLL",
        "COGNEE_IMPROVE_COOLDOWN",
        "COGNEE_IMPROVE_SUBMIT_TIMEOUT",
        "COGNEE_INJECT_MAX_CHARS",
        "COGNEE_LAZY_BOOTSTRAP",
        "COGNEE_LLM_OBSERVER",
        "COGNEE_LOG_EVENT_SCHEMA",
        "COGNEE_LOG_MAX_BYTES",
        "COGNEE_MANAGED_ENDPOINT",
        "COGNEE_OBSERVER_CONCURRENCY",
        "COGNEE_OBSERVER_MODEL",
        "COGNEE_OBSERVER_PORT",
        "COGNEE_OBSERVER_PROBE_TTL",
        "COGNEE_OBSERVER_RETIRE_AFTER",
        "COGNEE_OBSERVER_TIMEOUT",
        "COGNEE_PLUGIN_CITE_MEMORY",
        "COGNEE_PLUGIN_DATASET",
        "COGNEE_PLUGIN_LOG_MAX_BYTES",
        "COGNEE_PLUGIN_READ_DATASET_IDS",
        "COGNEE_PLUGIN_RECALL_MODE",
        "COGNEE_PLUGIN_VERBOSE",
        "COGNEE_PREFER_MEMORY",
        "COGNEE_PRESENCE_REPROBE_DELAY",
        "COGNEE_PROJECT_NODE_SET",
        "COGNEE_REACHABLE_TIMEOUT",
        "COGNEE_READY_PROBE_TIMEOUT",
        "COGNEE_RECALL_BUDGET",
        "COGNEE_RECALL_CONTEXT_CHARS",
        "COGNEE_RECALL_MIN_PROMPT_CHARS",
        "COGNEE_RECALL_PASSAGE_CHARS",
        "COGNEE_RECALL_PROJECT_NODE_SET",
        "COGNEE_RECALL_PROJECT_SCOPE",
        "COGNEE_RECALL_SHARED_NODE_SETS",
        "COGNEE_RECALL_SLOW_BUDGET",
        "COGNEE_RECALL_TIMEOUT",
        "COGNEE_REGISTER_TIMEOUT",
        "COGNEE_REMEMBER_BACKGROUND",
        "COGNEE_REMEMBER_TIMEOUT",
        "COGNEE_REMEMBER_WAIT_SECONDS",
        "COGNEE_REPORT_HINT",
        "COGNEE_SERVER_BOOT_DEADLINE",
        "COGNEE_SESSION_COMPANION_DATASET",
        "COGNEE_SESSION_PREFIX",
        "COGNEE_SESSION_STRATEGY",
        "COGNEE_SHARED_AGENT_MEMORY",
        "COGNEE_SHARED_MEMORY_REFRESH",
        "COGNEE_SLOW_STREAK_THRESHOLD",
        "COGNEE_SLOW_STREAK_WINDOW",
        "COGNEE_STATUSLINE_ACTIVITY",
        "COGNEE_STATUSLINE_COUNTS",
        "COGNEE_STATUSLINE_ICON",
        "COGNEE_STATUSLINE_STYLE",
        "COGNEE_STATUSLINE_THEME",
        "COGNEE_STATUS_REQUEST_TIMEOUT",
        "COGNEE_SWITCH_LOCK_TIMEOUT",
        "COGNEE_SYNC_RETRIES",
        "COGNEE_SYNC_RETRY_DELAY",
        "COGNEE_SYNC_START_DELAY",
        "COGNEE_UNREGISTER_ON_FINISH",
        "COGNEE_UPDATE_CHECK",
    }
)

# Claude Code exports the options to hook processes only (and their children);
# CLAUDE_PLUGIN_ROOT marks those. The status line and the Bash-run skills get
# no options, so they read the URL option where Claude Code keeps non-secret
# option values: ``pluginConfigs["cognee-memory@<marketplace>"].options`` in the
# user settings. The keys are secrets and are never readable there.
_HOOK_PROCESS_MARKER = "CLAUDE_PLUGIN_ROOT"
_PLUGIN_ID_PREFIX = "cognee-memory@"

# Explicit per-terminal mode switch. The shared name flips every Cognee plugin
# in the terminal; the plugin-specific name beats it when both are set. The
# plugin var is the ONE line that differs between the Claude Code and Codex
# copies of this module.
_PLUGIN_BACKEND_VAR = "COGNEE_CLAUDE_BACKEND"
_SHARED_BACKEND_VAR = "COGNEE_BACKEND"
_LOCAL_BACKEND_VALUES = ("local", "native", "sdk")
_CLOUD_BACKEND_VALUES = ("cloud", "http", "api", "server")


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


def refused(name: str) -> bool:
    """A credential or connection setting: taken from the plugin options only."""
    return (
        name in _REFUSED_EXACT
        or name.startswith(_REFUSED_PREFIXES)
        or bool(_SECRET_NAME.search(name))
    )


def _normalized_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def cloud_key_applies(service_url: str) -> bool:
    """Whether the Cloud key option is the key for ``service_url``.

    True for a remote server. A server on this machine gets its own owner key,
    never the Cloud key, with one exception: the terminal is pinned to cloud
    (``COGNEE_BACKEND=cloud``) **and** ``service_url`` is the configured Cloud
    URL option, a self-hosted server on a loopback address used as Cloud. The
    pin alone, with no URL option, sends the key nowhere.
    """
    url = _normalized_url(service_url)
    if not url:
        return False
    if not is_loopback_url(url):
        return True
    if forced_backend() != "cloud":
        return False
    configured = _normalized_url(url_option())
    return bool(configured) and configured == url


def cloud_pinned_without_url() -> bool:
    """The terminal is pinned to cloud (``COGNEE_BACKEND=cloud``) but the Cloud
    URL option is empty: there is no server to talk to. Nothing falls back to
    the local address, so no request is sent until the option is set."""
    return forced_backend() == "cloud" and not url_option()


def resolved_service_url() -> str:
    """The URL requests go to, as ``_plugin_common._local_api_url`` resolves it,
    or "" for the built-in local default."""
    local = os.environ.get(_LOCAL_URL_VAR, "").strip()
    return local or os.environ.get("COGNEE_BASE_URL", "").strip()


def _loadable(name: str, value: str) -> bool:
    """Whether the file may set ``name``: an allowlisted tuning name."""
    if refused(name):
        return False
    if name == _LOCAL_URL_VAR:
        return is_loopback_url(value)
    return name in _TUNING_NAMES


def option(name: str) -> str:
    """A plugin option's value as Claude Code exported it to this process, or ""."""
    return os.environ.get(OPTION_PREFIX + name.upper(), "").strip()


def cloud_key(service_url: str = "") -> str:
    """The Cloud key option when it is the key for ``service_url`` (default:
    the server requests go to), else "". Read from the option on every call."""
    key = option("cognee_api_key")
    if key and cloud_key_applies(service_url or resolved_service_url()):
        return key
    return ""


def server_secret_env() -> dict:
    """The LLM and embedding key options, under the names the local server
    reads them by. For the environment of the server this plugin starts only:
    they are never put in a plugin process's own environment."""
    return {
        OPTION_VARS[name]: option(name)
        for name in SENSITIVE_OPTIONS
        if name in LLM_OPTIONS and option(name)
    }


def is_hook_process() -> bool:
    return bool(os.environ.get(_HOOK_PROCESS_MARKER, "").strip())


def url_option() -> str:
    """The Cloud URL option: from the hook env, else from the saved settings."""
    if is_hook_process():
        return option(URL_OPTION)
    return _url_option_from_settings()


def _apply_plugin_options() -> None:
    """Take every credential and connection setting from the plugin options.

    Runs once per process, before the env file. The Cloud URL is derived again
    in every process; the keys are not put in the environment at all (see
    ``cloud_key`` and ``server_secret_env``). The non-secret ``LLM_*`` /
    ``EMBEDDING_*`` values are replaced only in the first plugin process of a
    chain (see ``_LLM_ENV_APPLIED``).
    """
    for var in _SHELL_DROPPED:
        os.environ.pop(var, None)
    for var in _STORAGE_DIR_VARS:
        # A value other than the plugin's own (which a child inherits from its
        # parent) came from the shell: dropped, and named for the notice.
        value = os.environ.get(var, "").strip()
        if value and value != _STORAGE_DIR_DEFAULTS[var]:
            os.environ.pop(var, None)
            if var not in _shell_storage_names:
                _shell_storage_names.append(var)
    if _shell_storage_names:
        carried = [
            n for n in os.environ.get(_SHELL_STORAGE_IGNORED_VAR, "").split(",") if n.strip()
        ]
        os.environ[_SHELL_STORAGE_IGNORED_VAR] = ",".join(
            sorted(set(carried) | set(_shell_storage_names))
        )
    local_url = os.environ.get(_LOCAL_URL_VAR, "")
    if local_url.strip() and not is_loopback_url(local_url):
        os.environ.pop(_LOCAL_URL_VAR, None)
    if os.environ.get(_LLM_ENV_APPLIED, "") != "1":
        for var in list(os.environ):
            if var.startswith(("LLM_", "EMBEDDING_")):
                os.environ.pop(var, None)
        for name, var in OPTION_VARS.items():
            value = option(name)
            if name in SENSITIVE_OPTIONS:
                continue
            if value and var.startswith(("LLM_", "EMBEDDING_")):
                os.environ[var] = value
        os.environ[_LLM_ENV_APPLIED] = "1"
    _apply_connection_options()


def _apply_connection_options() -> None:
    """The Cloud URL, from the option in every process.

    The Cloud key is not exported: ``cloud_key`` reads the option where a
    request needs it, and only for a server it is the key for.
    """
    base_url = url_option()
    if base_url:
        os.environ["COGNEE_BASE_URL"] = base_url
    else:
        os.environ.pop("COGNEE_BASE_URL", None)


def _url_option_from_settings() -> str:
    """The URL option as Claude Code saved it, for processes that get no options.

    Read from the user settings (``CLAUDE_CONFIG_DIR`` or ``~/.claude``), the
    only scope Claude Code reads ``pluginConfigs`` from besides managed settings.
    Never raises; "" when the plugin has no URL option set.
    """
    try:
        import json

        config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        root = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
        settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        configs = settings.get("pluginConfigs") if isinstance(settings, dict) else None
        for plugin_id, entry in (configs or {}).items():
            if not str(plugin_id).startswith(_PLUGIN_ID_PREFIX) or not isinstance(entry, dict):
                continue
            options = entry.get("options")
            value = options.get(URL_OPTION) if isinstance(options, dict) else None
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception:
        pass
    return ""


def _keeps_value(name: str, value: str) -> bool:
    """Whether the parser may keep ``name``'s value: only what the plugin uses."""
    if name == _LOCAL_URL_VAR:
        return is_loopback_url(_unquote(value))
    return name in _TUNING_NAMES and not refused(name)


def _parse(path: Path) -> tuple[dict, list, list]:
    """(values the plugin may use, refused names, other ignored names). Never raises.

    A value is kept only for an allowlisted tuning name (``_TUNING_NAMES``) or a
    loopback ``COGNEE_LOCAL_API_URL``. Every other line's value is dropped as
    soon as the line is split, whatever its name looks like, so it is never
    kept in any structure this module returns or holds. Refused names
    (credentials and connection settings, matched case-insensitively) and the
    remaining ignored names are recorded by name only.
    """
    values: dict[str, str] = {}
    refused_names: list[str] = []
    ignored_names: list[str] = []
    try:
        # utf-8-sig strips a UTF-8 BOM when present (Windows PowerShell 5.1
        # writes one) and is a no-op otherwise. A PS5 `>` redirect produces
        # UTF-16 instead — retry with that before giving up.
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-16")
    except Exception:
        return values, refused_names, ignored_names

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
        if _keeps_value(key, value):
            values[key] = _unquote(value)
            continue
        del value
        values.pop(key, None)
        names = refused_names if refused(key.upper()) else ignored_names
        if key not in names:
            names.append(key)
    return values, refused_names, ignored_names


def parse_env_file(path: Path) -> dict:
    """Parse a dotenv-style file into a dict, without any refused name's value.

    Supported: ``KEY=VALUE``, blank lines, ``#`` comments, an optional leading
    ``export ``, and single/double quotes around the value. No interpolation,
    no multi-line values. Malformed lines are skipped.
    """
    return _parse(path)[0]


def refused_names_in_file(path: Path | None = None) -> list:
    """Names of credential / connection settings the file holds (never values)."""
    path = path or env_file_path()
    if not path.is_file():
        return []
    return sorted(_parse(path)[1])


def _is_storage_name(name: str) -> bool:
    upper = name.upper()
    return upper in _STORAGE_NAMES or upper.startswith(_STORAGE_PREFIXES)


def storage_settings_ignored() -> list:
    """Storage setting names in ``~/.cognee/.env`` or the shell that the
    plugin's local server no longer uses (names only, never values)."""
    names = set(_shell_storage_names)
    names.update(
        n.strip()
        for n in os.environ.get(_SHELL_STORAGE_IGNORED_VAR, "").split(",")
        if n.strip() in _STORAGE_DIR_VARS
    )
    names.update(
        n
        for n in os.environ
        if n in ("DB_PROVIDER", "VECTOR_DB_PROVIDER", "GRAPH_DATABASE_PROVIDER")
    )
    try:
        path = env_file_path()
        if path.is_file():
            _values, refused_names, ignored_names = _parse(path)
            names.update(n for n in refused_names + ignored_names if _is_storage_name(n))
    except Exception:
        pass
    return sorted(names)


_loaded = False
# What this loader put into the environment (from the file, or the backend
# switch), key to value: what a refresh may change again. A real export is
# never among them, and a value changed by anything else since is left alone.
_injected: dict = {}
# The file as last parsed by a refresh (path, mtime, size): unchanged, it is not parsed again.
_seen: tuple = ()


def load_env_file() -> None:
    """Apply the plugin options, then the file's tuning settings. Never raises.

    Idempotent per process: repeated calls (this module is imported from
    several entry-point modules) parse the file at most once. The backend
    switch is applied afterwards — also when there is no file at all.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True

    _apply_plugin_options()
    try:
        path = env_file_path()
        if path.is_file():
            _tighten_permissions(path)
            for key, value in parse_env_file(path).items():
                if not _loadable(key, value):
                    continue
                if key not in os.environ:
                    os.environ[key] = value
                    _injected[key] = value
    except Exception:
        pass
    # The file may have set the local URL or the backend switch.
    _apply_connection_options()
    _apply_backend_switch()
    if plugin_disabled():
        # Every hook, command and the status line load their settings here:
        # stop before anything reaches a server or starts one.
        try:
            sys.stderr.write("cognee-plugin: disabled (COGNEE_PLUGIN_DISABLED)\n")
        except Exception:
            pass
        raise SystemExit(0)


_PLUGIN_DISABLED_VAR = "COGNEE_PLUGIN_DISABLED"


def plugin_disabled() -> bool:
    """``COGNEE_PLUGIN_DISABLED=true`` (shell or ``~/.cognee/.env``) turns the
    plugin off: no hook or command does anything. Used by the eval scaffold."""
    return os.environ.get(_PLUGIN_DISABLED_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def refresh_env_file() -> None:
    """Re-read the file's tuning settings in a long-lived process. Never raises.

    Values the file put in the environment follow the file (changed or gone);
    a real export still wins, as at load. Credentials and the Cloud URL never
    come from the file, so a refresh cannot change the connection.
    """
    global _seen
    if not _loaded:
        load_env_file()
        return
    try:
        path = env_file_path()
        # One stat per call; the file is parsed again only when it changed.
        stamp = (
            (str(path), path.stat().st_mtime_ns, path.stat().st_size)
            if path.is_file()
            else (str(path), 0, 0)
        )
        if stamp == _seen:
            return
        fresh = {
            key: value
            for key, value in (parse_env_file(path) if path.is_file() else {}).items()
            if _loadable(key, value)
        }
        _seen = stamp
    except Exception:
        return
    for key, last in list(_injected.items()):
        if os.environ.get(key) != last:
            # Set by something else since (an export, a test): theirs now.
            _injected.pop(key)
        elif key not in fresh:
            os.environ.pop(key, None)
            _injected.pop(key)
    for key, value in fresh.items():
        if key in _injected or key not in os.environ:
            os.environ[key] = value
            _injected[key] = value
    # A lifted backend switch left the connection empty, and a changed local
    # URL or switch changes which server requests go to: take the URL from
    # the options again (the switch, if still set, empties them once more).
    _apply_connection_options()
    _apply_backend_switch()


def forced_backend_with_source() -> tuple[str, str]:
    """The exported backend switch: ("local"|"cloud", var name), or ("", "").

    The plugin-specific variable beats the shared ``COGNEE_BACKEND``; a
    variable holding an unrecognized value is skipped rather than honored.
    """
    for var in (_PLUGIN_BACKEND_VAR, _SHARED_BACKEND_VAR):
        value = os.environ.get(var, "").strip().lower()
        if value in _LOCAL_BACKEND_VALUES:
            return "local", var
        if value in _CLOUD_BACKEND_VALUES:
            return "cloud", var
    return "", ""


def forced_backend() -> str:
    """ "local", "cloud", or "" — the exported backend switch, if any."""
    return forced_backend_with_source()[0]


def _apply_backend_switch() -> None:
    """Make a forced-local terminal actually local, everywhere.

    ``config.load_config`` clears base_url/api_key on the backend switch, but
    the HTTP hot paths (``_plugin_common``) and every child process read
    ``COGNEE_BASE_URL`` from the environment directly — so the switch must land
    in the environment itself, as empty strings.

    Forced cloud scrubs nothing: the cloud connection variables are exactly
    what that mode needs, and missing ones are surfaced by the status line
    rather than silently falling back to local.
    """
    if forced_backend() != "local":
        return
    os.environ["COGNEE_BASE_URL"] = ""
    # The loader's doing, so a refresh may undo it when the switch is lifted.
    _injected["COGNEE_BASE_URL"] = ""


def _tighten_permissions(path: Path) -> None:
    """Best-effort chmod 0600: an older file may still hold API keys."""
    if os.name == "nt":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.chmod(0o600)
    except Exception:
        pass


def legacy_settings_in_file() -> dict:
    """Cloud / LLM setting names the file still holds that the options lack.

    ``{"cloud": [...], "llm": [...]}``, names only: a group is listed only when
    the file has names for it and every matching option is empty. Session
    start shows a one-time upgrade notice for it.
    """
    names = refused_names_in_file()
    cloud = [n for n in names if n in ("COGNEE_BASE_URL", "COGNEE_API_KEY")]
    llm = [n for n in names if n.startswith(("LLM_", "EMBEDDING_"))]
    result: dict = {"cloud": [], "llm": []}
    if cloud and not (url_option() or option("cognee_api_key")):
        result["cloud"] = cloud
    if llm and not any(option(name) for name in LLM_OPTIONS):
        result["llm"] = llm
    return result


def env_file_status() -> dict:
    """Diagnostics for doctor: existence, perms, and key *names* (no values)."""
    path = env_file_path()
    info: dict = {"path": str(path), "exists": path.is_file()}
    mode, var = forced_backend_with_source()
    if mode:
        info["forced_backend"] = {"mode": mode, "var": var}
    if not info["exists"]:
        return info
    try:
        info["mode"] = oct(stat.S_IMODE(path.stat().st_mode))
    except Exception:
        pass
    values, refused_names, ignored_names = _parse(path)
    loadable = sorted(k for k, v in values.items() if _loadable(k, v))
    info["keys"] = loadable
    # Credentials and connection settings: set them in the plugin options.
    info["refused"] = sorted(refused_names)
    # Anything else the plugin does not read from the file.
    info["ignored"] = sorted(set(ignored_names) | {k for k in values if k not in loadable})
    # A shell export (or another source) that set a different value wins over
    # the file; surface those so mode-selection surprises are debuggable.
    info["overridden"] = sorted(k for k in loadable if os.environ.get(k, values[k]) != values[k])
    return info


if __name__ == "__main__":
    import json

    load_env_file()
    json.dump(env_file_status(), sys.stdout, indent=2)
    print()
