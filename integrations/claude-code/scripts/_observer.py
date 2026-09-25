"""Claude observer: run the local Cognee server's LLM calls on the Claude subscription.

Local mode needs an LLM for cognify/improve, and until now that meant an
``LLM_API_KEY`` for some provider — a second account and a second bill for
someone who already pays for Claude Code. The observer removes that
requirement: a small OpenAI-compatible HTTP shim (``claude-observer.py``)
runs on loopback and answers each ``/v1/chat/completions`` by invoking the
``claude`` CLI in headless mode (``claude -p --safe-mode``), which
authenticates with the credentials Claude Code already holds. The Cognee
server is pointed at that shim through cognee's own ``custom`` provider
(``LLM_PROVIDER=custom``, ``LLM_ENDPOINT=http://127.0.0.1:<port>/v1``), so
nothing in cognee changes — it sees an OpenAI-shaped endpoint.

Embeddings cannot come from the subscription (Claude has no embedding
endpoint), so the observer pairs the LLM shim with cognee's local CPU
embedder (``EMBEDDING_PROVIDER=fastembed``), whose driver extra session start
installs automatically.

Decision (``resolve_observer``), in order:

* ``COGNEE_LLM_OBSERVER=false`` → off.
* cloud mode → off (the remote server owns its LLM key).
* ``auto`` (default) and an ``LLM_API_KEY`` or an explicit ``LLM_PROVIDER`` is
  configured → off: the user chose a provider, respect it. That includes one in
  the ``.env`` the server itself loads (``server_dotenv_path``), which overrides
  anything this process exports.
* no ``claude`` executable reachable → off (``true`` surfaces this as an error).
* otherwise → on.

This module is stdlib-only and side-effect free on import. ``apply_observer_env``
is the only writer of the environment; it runs in SessionStart before the
cognee install/boot so the venv gets the fastembed extra and the server
inherits the provider variables.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SETTING_ENV = "COGNEE_LLM_OBSERVER"
#: Set by the shim in every ``claude`` child it spawns; every hook script exits
#: at once when it sees this, so an observer call can never re-enter the plugin.
CHILD_ENV_FLAG = "COGNEE_OBSERVER_CHILD"
#: Stamped into the environment by ``apply_observer_env`` so detached workers
#: (idle watcher, doctor) know the decision without recomputing it.
ACTIVE_ENV_FLAG = "COGNEE_LLM_OBSERVER_ACTIVE"

#: What cognee sees as the model. The ``openai/`` prefix routes litellm to the
#: OpenAI-compatible path against ``LLM_ENDPOINT``; the shim maps the alias to
#: the real Claude model (``COGNEE_OBSERVER_MODEL``).
MODEL_ALIAS = "claude-observer"
COGNEE_MODEL = f"openai/{MODEL_ALIAS}"
#: What older builds wrote as ``LLM_API_KEY``; still recognised as ours, never a
#: user key. The key cognee sends is now the shim's bearer token (``observer_token``).
PLACEHOLDER_KEY = "cognee-observer"
DEFAULT_PORT = 8017
DEFAULT_CLAUDE_MODEL = "haiku"
#: cognee's own keyless default (see its embeddings config): the smallest real
#: retrieval model in the fastembed registry. 384 dimensions.
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_EMBEDDING_DIMENSIONS = "384"

_STATE_DIR = Path.home() / ".cognee-plugin" / "observer"
PIDFILE = _STATE_DIR / "observer.pid"
LOG_FILE = _STATE_DIR / "observer.log"
#: Bearer token the shim requires on every request but ``/health``. It is stable
#: across launches (created once, mode 0600) rather than per launch: the shim and
#: the cognee server are shared by every session and restart independently, and a
#: server booted with an older token would otherwise be locked out of a new shim.
TOKEN_FILE = _STATE_DIR / "token"
_SCRIPT = Path(__file__).resolve().parent / "claude-observer.py"
#: The venv the local server runs from (``_plugin_common._VENV_DIR``).
_SERVER_VENV = Path.home() / ".cognee-plugin" / "venv"

_FALSE = {"0", "false", "no", "off"}
_TRUE = {"1", "true", "yes", "on"}


def setting() -> str:
    """``auto`` (default), ``true`` or ``false``."""
    raw = os.environ.get(SETTING_ENV, "").strip().lower()
    if raw in _FALSE:
        return "false"
    if raw in _TRUE:
        return "true"
    return "auto"


def port() -> int:
    try:
        value = int(os.environ.get("COGNEE_OBSERVER_PORT", "") or DEFAULT_PORT)
    except ValueError:
        value = DEFAULT_PORT
    return value if 0 < value < 65536 else DEFAULT_PORT


def base_url(port_number: int | None = None) -> str:
    return f"http://127.0.0.1:{port_number or port()}"


# What a ``--model`` value can look like: an alias (``haiku``, ``sonnet[1m]``),
# a full id (``claude-haiku-4-5-20251001``), a Vertex id (``claude-…@2025…``),
# a Bedrock id or ARN (``us.anthropic.…-v1:0``, ``arn:aws:bedrock:…/…``).
# Deliberately a shape check, not an allowlist of names: it rejects what cannot
# be a model — whitespace, a leading ``-`` (read as a flag) or ``.``, quotes,
# shell metacharacters — without breaking provider ids the CLI accepts.
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/\[\]-]{0,199}")


def valid_model(name: str) -> bool:
    return bool(_MODEL_RE.fullmatch(name or ""))


def _configured_model() -> str:
    return os.environ.get("COGNEE_OBSERVER_MODEL", "").strip()


def claude_model() -> str:
    """The ``--model`` for ``claude -p``; the default when the setting is unusable."""
    value = _configured_model()
    return value if valid_model(value) else DEFAULT_CLAUDE_MODEL


def model_warning() -> str:
    """Why ``COGNEE_OBSERVER_MODEL`` was ignored, or '' when it is usable/unset."""
    value = _configured_model()
    if not value or valid_model(value):
        return ""
    shown = value if len(value) <= 60 else value[:57] + "..."
    return (
        f"COGNEE_OBSERVER_MODEL={shown!r} is not a valid model name (use haiku, sonnet, "
        f"opus or a full model id); the observer uses {DEFAULT_CLAUDE_MODEL} instead."
    )


def find_claude() -> str:
    """Path of the ``claude`` executable, or ''.

    ``COGNEE_OBSERVER_CLAUDE`` pins one; otherwise PATH, then the places the
    official installers put it (the hook's PATH is the launching shell's, which
    on a GUI launch may be minimal).
    """
    pinned = os.environ.get("COGNEE_OBSERVER_CLAUDE", "").strip()
    if pinned and os.access(pinned, os.X_OK):
        return pinned
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "claude",
        home / ".claude" / "local" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidates.append(Path(appdata) / "npm" / "claude.cmd")
        candidates.append(home / ".local" / "bin" / "claude.exe")
    for candidate in candidates:
        if candidate.exists() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return ""


# An empty token file is only ever left by an interrupted writer (older builds
# created the file first and wrote it second). Younger than this, it may still
# be mid-write; older, it is abandoned and replaced.
_EMPTY_TOKEN_STALE_SECONDS = 5.0


def _read_token() -> str | None:
    """The stored token ('' for an empty file), or None when there is no file."""
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None


def _publish_token() -> str:
    """Create the token file atomically and return the token it holds.

    The token is written to a private temp file first and hard-linked into
    place: ``os.link`` fails when the target exists, so exactly one writer
    wins, and the file is never visible without its content — a concurrent
    reader cannot see it empty. A loser returns the winner's token.
    """
    fd, tmp = tempfile.mkstemp(prefix=".token-", dir=str(_STATE_DIR))  # 0600
    try:
        token = secrets.token_urlsafe(32)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
        try:
            os.link(tmp, TOKEN_FILE)
            return token
        except FileExistsError:
            return _read_token() or ""
        except OSError:
            # No hard links on this filesystem: exclusive create, then write.
            # Not atomic, but the empty-file recovery in observer_token covers it.
            try:
                out = os.open(str(TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return _read_token() or ""
            with os.fdopen(out, "w", encoding="utf-8") as fh:
                fh.write(token)
            return token
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def observer_token(create: bool = True) -> str:
    """The shim's bearer token ('' when absent and ``create`` is False). Never raises."""
    try:
        token = _read_token()
        if token or not create:
            return token or ""
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        if token is None:
            return _publish_token()
        # An empty file: a writer mid-write, or one that died before writing.
        for _ in range(5):
            time.sleep(0.1)
            token = _read_token()
            if token is None:
                return _publish_token()
            if token:
                return token
        try:
            age = time.time() - TOKEN_FILE.stat().st_mtime
        except FileNotFoundError:
            return _publish_token()
        if age < _EMPTY_TOKEN_STALE_SECONDS:
            return ""
        # Abandoned. Best effort: two processes recovering the same abandoned
        # file at the same instant can still end up holding different tokens.
        try:
            TOKEN_FILE.unlink()
        except FileNotFoundError:
            pass
        return _publish_token()
    except OSError:
        return ""


def _auth_headers() -> dict:
    token = observer_token(create=False)
    return {"Authorization": f"Bearer {token}"} if token else {}


def _is_observer_key(key: str) -> bool:
    return key == PLACEHOLDER_KEY or (bool(key) and key == observer_token(create=False))


def llm_key_configured() -> bool:
    key = os.environ.get("LLM_API_KEY", "").strip()
    return bool(key) and not _is_observer_key(key)


def server_dotenv_path() -> Path | None:
    """The ``.env`` the local cognee server will load, if any.

    ``import cognee`` runs ``dotenv.load_dotenv(override=True)``, whose
    ``find_dotenv`` walks up from the cognee package directory and takes the
    first ``.env`` it finds: inside the venv, then ``~/.cognee-plugin``, ``~``,
    and so on to the root. Because of ``override=True`` its values beat the
    environment the server was spawned with, so a key there is the one the
    server uses even though this process never sees it.
    """
    start = _SERVER_VENV
    for pattern in ("lib/python*/site-packages/cognee", "Lib/site-packages/cognee"):
        found = sorted(_SERVER_VENV.glob(pattern))
        if found:
            start = found[-1]
            break
    try:
        current = start.resolve()
    except OSError:
        current = start
    for directory in (current, *current.parents):
        candidate = directory / ".env"
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def server_dotenv_values() -> dict:
    path = server_dotenv_path()
    if path is None:
        return {}
    try:
        from _env_file import parse_env_file

        return parse_env_file(path)
    except Exception:
        return {}


def _is_local_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host in ("localhost", "::1", "0.0.0.0") or host.startswith("127.")


def llm_provider_configured() -> bool:
    """An explicit, non-observer provider choice (e.g. ``LLM_PROVIDER=ollama``)."""
    provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if not provider:
        return False
    if provider == "custom" and os.environ.get(ACTIVE_ENV_FLAG, "") == "1":
        return False
    return True


def resolve_observer(config: dict | None = None) -> dict:
    """Decide whether the observer serves this launch. Never raises.

    Returns ``{"active", "setting", "reason", "claude", "port", "endpoint",
    "model", "error"}``, plus ``model_warning`` when ``COGNEE_OBSERVER_MODEL`` was
    unusable and ``model`` fell back to the default. ``reason`` explains an
    inactive verdict; ``error`` is set only when ``COGNEE_LLM_OBSERVER=true``
    cannot be honoured.
    """
    config = config or {}
    mode = setting()
    result = {
        "active": False,
        "setting": mode,
        "reason": "",
        "claude": find_claude(),
        "port": port(),
        "endpoint": base_url() + "/v1",
        "model": claude_model(),
        "error": "",
    }
    if mode == "false":
        result["reason"] = "disabled"
        return result
    # SessionStart fills ``base_url`` with the local server's URL before asking,
    # so only a non-loopback URL (or the forced cloud backend) means cloud.
    base = str(config.get("base_url") or "").strip()
    cloud = (bool(base) and not _is_local_url(base)) or (config.get("_forced_backend") == "cloud")
    if cloud:
        result["reason"] = "cloud_mode"
        return result
    if mode == "auto":
        # Do not steal a launch the user configured for a provider of their own.
        # A prior apply in this same process (or the bootstrap re-exec) stamped
        # the active flag, in which case the placeholder key/custom provider are
        # ours and not a user choice.
        if os.environ.get(ACTIVE_ENV_FLAG, "") != "1":
            if llm_key_configured():
                result["reason"] = "llm_key_configured"
                return result
            if llm_provider_configured():
                result["reason"] = "llm_provider_configured"
                return result
            dotenv = server_dotenv_values()
            dotenv_key = str(dotenv.get("LLM_API_KEY") or "").strip()
            if (dotenv_key and not _is_observer_key(dotenv_key)) or str(
                dotenv.get("LLM_PROVIDER") or ""
            ).strip():
                result["reason"] = "server_dotenv_configured"
                result["dotenv"] = str(server_dotenv_path() or "")
                return result
    warning = model_warning()
    if warning:
        result["model_warning"] = warning
    if not result["claude"]:
        result["reason"] = "claude_cli_missing"
        if mode == "true":
            result["error"] = (
                "COGNEE_LLM_OBSERVER=true but no `claude` executable was found on PATH; "
                "set COGNEE_OBSERVER_CLAUDE=/path/to/claude or install Claude Code's CLI."
            )
        return result
    result["active"] = True
    return result


def apply_observer_env(decision: dict) -> dict:
    """Point cognee at the shim. Returns the variables written (for logging).

    Called in the SessionStart process BEFORE the cognee install and server
    boot: ``_detect_required_extras`` reads ``EMBEDDING_PROVIDER`` to add the
    fastembed extra, and the server inherits ``os.environ`` at spawn. The LLM
    variables are overwritten — an ``LLM_MODEL`` left in the env file without
    a key would not work anyway — while the embedding ones are ``setdefault``,
    so a user who configured their own (say Ollama) embedder keeps it.
    """
    if not decision.get("active"):
        return {}
    applied = {
        "LLM_PROVIDER": "custom",
        "LLM_MODEL": COGNEE_MODEL,
        "LLM_ENDPOINT": decision.get("endpoint") or (base_url() + "/v1"),
        # The shim's bearer token: litellm sends it as ``Authorization: Bearer``.
        "LLM_API_KEY": observer_token() or PLACEHOLDER_KEY,
        ACTIVE_ENV_FLAG: "1",
    }
    for key, value in applied.items():
        os.environ[key] = value
    for key, value in (
        ("EMBEDDING_PROVIDER", "fastembed"),
        ("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        ("EMBEDDING_DIMENSIONS", DEFAULT_EMBEDDING_DIMENSIONS),
    ):
        if not os.environ.get(key, "").strip():
            os.environ[key] = value
            applied[key] = value
    return applied


def is_active() -> bool:
    """True when this launch's server LLM is the observer.

    Fast path: the environment ``apply_observer_env`` prepared (SessionStart
    itself and every worker it spawned). Hooks run in the host's environment,
    which has none of that — so a watcher re-spawned from a hook falls back to
    the launch record SessionStart stamped (``llm_observer.active``). Without
    the fallback that watcher would run the litellm key check against a launch
    that has no key by design and put a false ✕ on the status line.
    """
    if os.environ.get(ACTIVE_ENV_FLAG, "") == "1":
        return True
    try:
        from _plugin_common import _read_map_record, get_session_key

        host_key = get_session_key()
        if not host_key:
            return False
        marker = _read_map_record(host_key).get("llm_observer")
        return bool(isinstance(marker, dict) and marker.get("active"))
    except Exception:
        return False


# --- shim process control ----------------------------------------------------


def _http_get(url: str, timeout: float) -> tuple[int, dict]:
    try:
        req = urllib.request.Request(url, headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, (json.loads(body) if body else {})
        except Exception:
            return exc.code, {}
    except Exception:
        return 0, {}


def observer_health(port_number: int | None = None, timeout: float = 1.0) -> dict:
    """The shim's ``/health`` document, or {} when nothing of ours answers there."""
    status, body = _http_get(base_url(port_number) + "/health", timeout)
    if status == 200 and isinstance(body, dict) and body.get("service") == "cognee-claude-observer":
        return body
    return {}


def observer_alive(port_number: int | None = None, timeout: float = 1.0) -> bool:
    return bool(observer_health(port_number, timeout))


def observer_probe(
    port_number: int | None = None, timeout: float = 60.0, force: bool = False
) -> dict:
    """Ask the shim whether ``claude`` can actually answer (auth verdict).

    Returns ``{"auth": "ok"|"failed"|"unknown", "detail": str, "status": int}``.
    The shim caches its verdict, so this is cheap after the first call.
    """
    url = base_url(port_number) + "/v1/observer/probe" + ("?force=1" if force else "")
    status, body = _http_get(url, timeout)
    if not isinstance(body, dict):
        body = {}
    auth = str(body.get("auth") or ("ok" if status == 200 else "unknown"))
    if status == 401:
        auth = "failed"
    elif status == 0:
        auth = "unknown"
    return {"auth": auth, "detail": str(body.get("detail") or ""), "status": status}


def _read_pid() -> int:
    try:
        return int(PIDFILE.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return 0


def ensure_observer_running(
    decision: dict,
    *,
    cognee_url: str = "",
    wait: float = 4.0,
    python: str = "",
) -> bool:
    """Start the shim if nothing answers on its port. Idempotent; never raises.

    Detached (own session, fds closed) so it outlives the hook, like the idle
    watcher. Stdout/stderr go to ``observer.log``. ``cognee_url`` tells the shim
    which server it serves so it can retire itself once that server is gone.
    """
    if not decision.get("active"):
        return False
    port_number = int(decision.get("port") or port())
    if observer_alive(port_number):
        return True
    if not _SCRIPT.exists():
        return False
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        log_fh = LOG_FILE.open("a", encoding="utf-8")
    except Exception:
        log_fh = subprocess.DEVNULL
    observer_token()  # the shim reads it at start; make sure it exists first
    env = os.environ.copy()
    if decision.get("claude"):
        env["COGNEE_OBSERVER_CLAUDE"] = str(decision["claude"])
    args = [
        python or sys.executable,
        str(_SCRIPT),
        "serve",
        "--port",
        str(port_number),
    ]
    if cognee_url:
        args += ["--cognee-url", cognee_url]
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        return False
    deadline = time.monotonic() + max(0.0, wait)
    while time.monotonic() < deadline:
        if observer_alive(port_number, timeout=0.5):
            return True
        time.sleep(0.15)
    return observer_alive(port_number, timeout=0.5)


def stop_observer(port_number: int | None = None) -> bool:
    """Ask a running shim to exit (``POST /v1/observer/shutdown``), else SIGTERM the pidfile."""
    url = base_url(port_number) + "/v1/observer/shutdown"
    try:
        req = urllib.request.Request(url, data=b"{}", method="POST", headers=_auth_headers())
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=2.0):
            return True
    except Exception:
        pass
    pid = _read_pid()
    if pid > 1:
        try:
            import signal

            os.kill(pid, signal.SIGTERM)
            return True
        except Exception:
            return False
    return False


def describe(decision: dict | None = None) -> str:
    """One line for doctor / session start: what the observer is doing and why."""
    decision = decision or resolve_observer()
    if decision.get("active"):
        return (
            f"on — local server LLM calls run through Claude Code "
            f"(`claude -p`, model {decision.get('model')}) at {decision.get('endpoint')}; "
            f"embeddings on fastembed"
        )
    reasons = {
        "disabled": f"off ({SETTING_ENV}=false)",
        "cloud_mode": "off (cloud mode: the remote server owns its LLM key)",
        "llm_key_configured": "off (LLM_API_KEY is configured)",
        "llm_provider_configured": "off (LLM_PROVIDER is configured)",
        "server_dotenv_configured": (
            f"off (the server's {decision.get('dotenv') or '.env'} configures its LLM)"
        ),
        "claude_cli_missing": "unavailable (no `claude` executable found)",
    }
    return reasons.get(str(decision.get("reason") or ""), "off")


#: Shown whenever the observer serves a launch (session start, doctor, README).
SPEND_WARNING = (
    "Cognee's cognify/improve calls run on your Claude subscription through "
    "`claude -p` and count against your Claude usage limits. Building the graph "
    "is token-heavy."
)
#: Embedding vectors from different models cannot be compared, so a dataset is
#: tied to the embedder that built it.
EMBEDDING_WARNING = (
    "Embeddings: {provider} ({model}, {dims} dims). A dataset built "
    "with one embedding model cannot be searched with another: if you later set "
    "LLM_API_KEY or change EMBEDDING_*, switch to a new dataset "
    "(/cognee-memory:cognee-switch-datasets or COGNEE_PLUGIN_DATASET) instead of "
    "reusing this one."
)


def embedding_warning() -> str:
    return EMBEDDING_WARNING.format(
        provider=os.environ.get("EMBEDDING_PROVIDER") or "fastembed",
        model=os.environ.get("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL,
        dims=os.environ.get("EMBEDDING_DIMENSIONS") or DEFAULT_EMBEDDING_DIMENSIONS,
    )
