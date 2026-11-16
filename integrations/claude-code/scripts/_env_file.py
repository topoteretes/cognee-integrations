"""Plugin configuration: keys from the plugin options, the rest from ~/.cognee/.env.

Every key comes from the plugin's options (the ``userConfig`` section of the
plugin manifest): the Cognee API key, the LLM key and the embedding
key. Claude Code keeps them in its secure store and hands them to the plugin's
hooks, and to the processes those hooks start, as ``CLAUDE_PLUGIN_OPTION_*``.
The same holds for the login of a server that has no API key (an email and a
password option), and the server URL has an option of its own.
The plugin reads a key nowhere else: a value for one of the names in
``OPTION_ONLY`` in the shell or in the file is dropped and never used.
A process Claude Code gives no options (a script a skill runs through Bash)
has no key; it hands its work to the relay a hook started (``_relay.py``).

Everything that is not a key is a one-time step here: a value placed in
``~/.cognee/.env`` (the durable, venv-rebuild-safe cognee home shared by the
Claude Code and Codex plugins) acts like a shell export for the plugin:

  real exported variables  >  ~/.cognee/.env  >  defaults

Each setting the plugin's scripts read is handled by its own name, from a
generated list (``_env_names.py``), so the plugin's files show every variable
the file can set for it. Anything else in the file (database, vector store and
other cognee settings) is for the local Cognee server: it is handed to that
server when the plugin starts it (``passthrough_env``) and never enters the
plugin's own environment.

Deliberately stdlib-only: hook scripts must run outside the plugin venv on the
cloud path, so python-dotenv is not an option. The parser accepts a leading
``export `` so users can paste their existing shell export lines verbatim.

The file may hold BOTH modes' variables at once (cloud connection + local LLM
key); with nothing exported, cloud wins because ``COGNEE_BASE_URL`` routes the
connection. One export flips a single terminal: ``COGNEE_BACKEND=local`` (or
``=cloud``), with the plugin-specific variable (``_PLUGIN_BACKEND_VAR``)
beating the shared name. A forced-local switch is applied to the process
environment itself (see ``_apply_backend_switch``) so the HTTP hot paths and spawned
children — which read ``COGNEE_BASE_URL`` from the environment directly, not
via ``config.load_config`` — see the same mode.

Loading must never break a hook: any parse or IO problem results in the file
being (partially) ignored, never an exception.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

_COGNEE_HOME = Path.home() / ".cognee"
_DEFAULT_ENV_FILE = _COGNEE_HOME / ".env"

# The keys, which come from the plugin options only: never loaded from the
# file and removed from the environment the process was started with.
OPTION_ONLY = (
    "COGNEE_API_KEY",
    "COGNEE_PRINCIPAL_API_KEY",
    "LLM_API_KEY",
    "EMBEDDING_API_KEY",
    # The login for a server without an API key: two options as well.
    "COGNEE_USER_EMAIL",
    "COGNEE_USER_PASSWORD",
)
# Where a hook records the server URL option, for the processes Claude Code
# gives no options (the status line): an address, not a secret.
_URL_RECORD = Path.home() / ".cognee-plugin" / "claude-code" / "server-url.json"
# Which of those names this process found in the shell or in the file (names
# only, never a value): what the session-start notice and doctor report.
_ignored_key_names: list = []
# The same names, carried to the processes this one starts: they see an
# environment the names were already removed from.
_IGNORED_KEYS_VAR = "COGNEE_PLUGIN_IGNORED_SETTINGS"

# Vars that could hijack process behavior if injected from a config file.
# Everything else (COGNEE_*, LLM_*, EMBEDDING_*, ...) is allowed so the values
# also flow through to the spawned local cognee server.
_DENYLIST_EXACT = {"PATH", "HOME", "PYTHONPATH", "PYTHONHOME", "SHELL", "USER"}
_DENYLIST_PREFIXES = ("LD_", "DYLD_")

# Explicit per-terminal mode switch. The shared name flips every Cognee plugin
# in the terminal; the plugin-specific name beats it when both are set. The
# plugin var is the ONE line that differs between the Claude Code and Codex
# copies of this module.
_PLUGIN_BACKEND_VAR = "COGNEE_CLAUDE_BACKEND"
_SHARED_BACKEND_VAR = "COGNEE_BACKEND"
_LOCAL_BACKEND_VALUES = ("local", "native", "sdk")
_CLOUD_BACKEND_VALUES = ("cloud", "http", "api", "server")

_TEMPLATE = """\
# Cognee plugin configuration — shared by the Claude Code and Codex plugins.
# Values here are loaded at session start and act like shell exports, except
# you only set them once. A real `export` in your shell still wins over this
# file. Lines starting with `#` are comments; a leading `export ` is allowed.
#
# You can fill in BOTH modes below. When both are configured, cloud wins.
# To pick a mode for a single terminal, export the switch before launching:
#   export COGNEE_BACKEND=local    # this terminal: local mode
#   export COGNEE_BACKEND=cloud    # this terminal: cloud mode
# (COGNEE_CLAUDE_BACKEND / COGNEE_CODEX_BACKEND target one plugin only.)

## Keys: the Claude Code plugin takes its keys from its own options, not from
## this file (run /plugin in Claude Code, select Cognee Memory, then Configure
## options). A key written here is ignored by the Claude Code plugin; the Codex
## plugin still reads it:
# COGNEE_API_KEY="ck_..."
# LLM_API_KEY="sk-..."

## Cloud / remote mode — point the plugins at a Cognee instance:
# COGNEE_BASE_URL="<your Cognee Cloud URL>"

## Local mode (default when COGNEE_BASE_URL is unset) — the plugin runs a
## local Cognee API, with an LLM key of your own:
# LLM_MODEL="openai/gpt-4o-mini"
## ...or give it no LLM key: with the `claude` CLI on PATH the Claude Code
## plugin runs the server's LLM calls on your Claude subscription instead (the
## "observer"; embeddings then run locally on fastembed). Claude Code only.
## It spends your Claude usage limits, and a dataset built on fastembed cannot
## be searched after switching to a key's embedder: switch datasets then.
# COGNEE_LLM_OBSERVER="auto"     # auto | true | false
# COGNEE_OBSERVER_MODEL="haiku"  # haiku | sonnet | opus | full model id

## Local mode backends (optional) — the defaults (sqlite/lancedb/openai) need
## nothing here. Configuring a provider below makes session start install the
## matching cognee driver extra into the plugin venv automatically:
# DB_PROVIDER="postgres"
# VECTOR_DB_PROVIDER="pgvector"
# GRAPH_DATABASE_PROVIDER="neo4j"
# EMBEDDING_PROVIDER="fastembed"
# LLM_PROVIDER="ollama"

## Optional:
# COGNEE_PLUGIN_DATASET="agent_sessions"
# COGNEE_PLUGIN_RECALL_MODE="auto"   # auto | pinned (recall only from the dataset above)
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


def _blocked(name: str) -> bool:
    return name in _DENYLIST_EXACT or name.startswith(_DENYLIST_PREFIXES) or name in OPTION_ONLY


def login_email_option() -> str:
    """The login email option (a server without an API key), or ""."""
    return os.environ.get("CLAUDE_PLUGIN_OPTION_COGNEE_USER_EMAIL", "").strip()


def login_password_option() -> str:
    """The login password option, or ""."""
    return os.environ.get("CLAUDE_PLUGIN_OPTION_COGNEE_USER_PASSWORD", "").strip()


def _in_hook() -> bool:
    """Whether Claude Code started this process as a hook, or a hook started it:
    those are the processes that have the plugin's options."""
    return bool(os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip())


def _apply_url_option() -> None:
    """Take the server URL from its option when the shell exports none.

    Order: a real ``COGNEE_BASE_URL`` export, then the option, then
    ~/.cognee/.env. A hook records the option's value so that a process with
    no options (the status line) resolves the same server; an emptied option
    removes the record.
    """
    if os.environ.get("COGNEE_BASE_URL", "").strip():
        return
    if _in_hook():
        url = os.environ.get("CLAUDE_PLUGIN_OPTION_COGNEE_BASE_URL", "").strip()
        try:
            recorded = json.loads(_URL_RECORD.read_text(encoding="utf-8")).get("url", "")
        except Exception:
            recorded = ""
        if url != recorded:
            try:
                if url:
                    _URL_RECORD.parent.mkdir(parents=True, exist_ok=True)
                    _URL_RECORD.write_text(json.dumps({"url": url}), encoding="utf-8")
                else:
                    _URL_RECORD.unlink()
            except Exception:
                pass
    else:
        try:
            url = str(json.loads(_URL_RECORD.read_text(encoding="utf-8")).get("url") or "")
        except Exception:
            url = ""
    if url:
        # Like an export: the file's own value does not replace it.
        os.environ["COGNEE_BASE_URL"] = url


def cloud_key_option() -> str:
    """The Cognee API key option as Claude Code handed it to this process, or ""."""
    return os.environ.get("CLAUDE_PLUGIN_OPTION_COGNEE_API_KEY", "").strip()


def llm_key_option() -> str:
    """The LLM key option (local mode), or ""."""
    return os.environ.get("CLAUDE_PLUGIN_OPTION_LLM_API_KEY", "").strip()


def embedding_key_option() -> str:
    """The embedding key option (local mode), or ""."""
    return os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_API_KEY", "").strip()


def local_models_option() -> bool:
    """Whether the user chose to build memory without an LLM key, on cognee's
    local models (GLiNER and fastembed). Off unless the option says so."""
    value = os.environ.get("CLAUDE_PLUGIN_OPTION_LOCAL_MODELS", "").strip().lower()
    return value in ("1", "true", "yes", "on")


def has_options() -> bool:
    """Whether Claude Code handed this process the plugin's options: true in a
    hook and in what a hook started, false in a script run from a shell."""
    return bool(os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip())


def server_key_env() -> dict:
    """The LLM and embedding key options under the names the local server reads
    them by. Only for the environment of the server this plugin starts; a
    plugin process never carries them in its own."""
    wanted = {"LLM_API_KEY": llm_key_option(), "EMBEDDING_API_KEY": embedding_key_option()}
    return {name: value for name, value in wanted.items() if value}


def ignored_key_names() -> list:
    """Names of the keys found in the shell or in ~/.cognee/.env and ignored."""
    held = os.environ.get("COGNEE_PLUGIN_IGNORED_SETTINGS", "")
    carried = [n for n in held.split(",") if n.strip()]
    return sorted(set(carried) | set(_ignored_key_names))


def _note_ignored(name: str) -> None:
    if name not in _ignored_key_names:
        _ignored_key_names.append(name)
    os.environ["COGNEE_PLUGIN_IGNORED_SETTINGS"] = ",".join(ignored_key_names())


def _drop_shell_keys() -> None:
    """Remove the keys the process was started with: the options replace them."""
    # Each by its own name, so the plugin's files show what it touches.
    if os.environ.pop("COGNEE_API_KEY", ""):
        _note_ignored("COGNEE_API_KEY")
    if os.environ.pop("COGNEE_PRINCIPAL_API_KEY", ""):
        _note_ignored("COGNEE_PRINCIPAL_API_KEY")
    if os.environ.pop("LLM_API_KEY", ""):
        _note_ignored("LLM_API_KEY")
    if os.environ.pop("EMBEDDING_API_KEY", ""):
        _note_ignored("EMBEDDING_API_KEY")
    if os.environ.pop("COGNEE_USER_EMAIL", ""):
        _note_ignored("COGNEE_USER_EMAIL")
    if os.environ.pop("COGNEE_USER_PASSWORD", ""):
        _note_ignored("COGNEE_USER_PASSWORD")


# One setting: NAME=VALUE, with the optional leading word a shell profile line
# carries, so such lines paste verbatim.
_SETTING_LINE = re.compile(r"^(?:export\s+)?([A-Za-z0-9_]+)\s*=(.*)$")


def parse_env_file(path: Path, key_names: list | None = None, not_keys: tuple = ()) -> dict:
    """Parse a dotenv-style file into a dict. Malformed lines are skipped.

    A key (``OPTION_ONLY``) is never returned with its value. Pass a list as
    ``key_names`` to learn which keys the file sets: their names are appended.
    A value listed in ``not_keys`` (a token of the plugin's own) does not count.

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
        parsed = _SETTING_LINE.match(line)
        if not parsed:
            continue
        name, value = parsed.group(1), parsed.group(2)
        # A key's value is dropped as it is parsed: only its name is kept, so
        # the notice can say that it is ignored.
        if name in OPTION_ONLY:
            held = _unquote(value)
            if key_names is not None and held and held not in not_keys and name not in key_names:
                key_names.append(name)
            result[name] = ""
            continue
        result[name] = _unquote(value)
    return result


_loaded = False
# What this loader put into the environment (from the file, or the backend
# switch), key to value: what a refresh may change again. A real export is
# never among them, and a value changed by anything else since is left alone.
_injected: dict = {}
# The file's usable values as last read (never a key: the parser drops those).
_file_values: dict = {}
# The file as last parsed by a refresh (path, mtime, size): unchanged, it is not parsed again.
_seen: tuple = ()


def _take(values: dict) -> None:
    """Make this process follow the file's ``values``.

    The settings the plugin's scripts read are put in the environment, each by
    its own name (``_env_names.sync``, a generated list): when the shell
    exports none, and following the file when it changes. Anything else the
    file holds is kept aside for the local server (``passthrough_env``) and
    never enters the plugin's own environment.
    """
    import _env_names

    usable = {name: value for name, value in values.items() if not _blocked(name)}
    _file_values.clear()
    _file_values.update(usable)
    _env_names.sync(usable, _injected)


def passthrough_env() -> dict:
    """What the file holds for programs the plugin starts, the local Cognee
    server above all: every setting the plugin does not read itself (database,
    vector store and other cognee settings). The caller adds them to that
    program's environment where the shell exports nothing of the same name."""
    import _env_names

    return {name: value for name, value in _file_values.items() if name not in _env_names.NAMES}


def load_env_file() -> None:
    """Load the env file for this process (a real export wins). Never raises.

    Idempotent per process: repeated calls (this module is imported from
    several entry-point modules) parse the file at most once. The backend
    switch is applied afterwards — and also when there is no file at all, so
    ``COGNEE_BACKEND=local`` beats a ``COGNEE_BASE_URL`` exported in the shell
    the same way it beats one defined in the file.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    # A load is this process's first look at the file: nothing is its doing yet.
    _injected.clear()

    _drop_shell_keys()
    try:
        _apply_url_option()
    except Exception:
        pass
    try:
        path = env_file_path()
        if path.is_file():
            _tighten_permissions(path)
            named: list = []
            values = parse_env_file(path, named)
            for name in named:
                _note_ignored(name)
            _take(values)
    except Exception:
        pass
    _apply_backend_switch()


def refresh_env_file() -> None:
    """Re-read the file in a long-lived process. Never raises.

    Hooks are short processes and load the file at start, so an edit reaches
    the next hook. The cloud worker lives for hours: without this it kept the
    tenant it started with and never served a session pointed elsewhere.
    Values the file put in the environment follow the file (changed or gone);
    a real export still wins, as at load.
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
        fresh = parse_env_file(path) if path.is_file() else {}
        _seen = stamp
    except Exception:
        return
    _take(fresh)
    _apply_backend_switch()


def forced_backend_with_source() -> tuple[str, str]:
    """The exported backend switch: ("local"|"cloud", var name), or ("", "").

    The plugin-specific variable beats the shared ``COGNEE_BACKEND``; a
    variable holding an unrecognized value is skipped rather than honored.
    """
    for var, raw in (
        (_PLUGIN_BACKEND_VAR, os.environ.get("COGNEE_CLAUDE_BACKEND", "")),
        (_SHARED_BACKEND_VAR, os.environ.get("COGNEE_BACKEND", "")),
    ):
        value = raw.strip().lower()
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

    ``config.load_config`` clears base_url on the backend switch, but
    the HTTP hot paths (``_plugin_common``) and every child process read
    ``COGNEE_BASE_URL`` from the environment directly — so the switch must land
    in the environment itself. Overwrite with EMPTY strings rather than
    deleting: a child re-running this loader must not re-inject the file's
    cloud values (setdefault skips keys that are present, even when empty).

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
    """Best-effort chmod 0600: the file is the user's private configuration."""
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
    mode, var = forced_backend_with_source()
    if mode:
        info["forced_backend"] = {"mode": mode, "var": var}
    if not info["exists"]:
        return info
    try:
        info["mode"] = oct(stat.S_IMODE(path.stat().st_mode))
    except Exception:
        pass
    values = parse_env_file(path)
    info["keys"] = sorted(values)
    info["blocked"] = sorted(k for k in values if _blocked(k) and k not in OPTION_ONLY)
    info["ignored_keys"] = ignored_key_names()
    # A shell export (or another source) that set a different value wins over
    # the file; surface those so mode-selection surprises are debuggable.
    import _env_names

    info["overridden"] = sorted(
        _env_names.overridden({k: v for k, v in values.items() if not _blocked(k)})
    )
    # Settings for the local server only: never in the plugin's environment.
    info["passed_to_server"] = sorted(
        k for k in values if not _blocked(k) and k not in _env_names.NAMES
    )
    return info


if __name__ == "__main__":
    import json

    load_env_file()
    json.dump(env_file_status(), sys.stdout, indent=2)
    print()
