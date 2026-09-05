"""Shared configuration for the Cognee Antigravity plugin.

Loads settings from (in priority order):
  1. Environment variables (runtime overrides)
  2. Env file (~/.cognee/.env — one-time setup, injected into os.environ
     with setdefault, so it sits just below real shell exports)
  3. Defaults

There is deliberately no config file. An earlier ``~/.cognee-plugin/config.json``
layer was read by SessionStart but not by the per-turn hooks, so a stale
``base_url`` in it could point the two halves of the plugin at different servers
(SDK-466); the env file covers every key it held, from one place every process
reads. SessionStart deletes a leftover file so it cannot mislead anyone.

The env file may hold both modes' variables at once; cloud wins when both are
configured. `export COGNEE_BACKEND=local` (or `=cloud`) flips one terminal —
COGNEE_ANTIGRAVITY_BACKEND does the same for this plugin only, beating the shared
name. A forced mode is pinned: forced local scrubs the cloud connection vars
from the process environment (see _env_file), and forced cloud keeps
is_cloud_mode() true even when connection vars are missing, so the plugin
attempts the cloud connection and the status line reports what is wrong
instead of silently falling back to local.

Supports three modes:
  - Local: Cognee runs in-process (SQLite + LanceDB + Kuzu)
  - Cloud: Connect to Cognee Cloud via cognee.serve()
  - Server: Legacy — direct base_url (kept for backward compat)
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from _env_file import load_env_file
from _logfiles import append_line as _append_log_line
from event_names import event_fields

# Must run before the _ENV_MAP scan in load_config() and before any importer's
# module-level os.environ reads.
load_env_file()

_STATE_DIR = Path.home() / ".cognee-plugin" / "antigravity"
_HOOK_LOG = _STATE_DIR / "hook.log"

_DEFAULTS = {
    "dataset": "agent_sessions",
    "agent_name": "antigravity-agent",
    "session_strategy": "per-directory",  # per-directory | git-branch | static
    "session_prefix": "antigravity",
    "top_k": 3,
    "backend": "auto",
    "user_email": "default_user@example.com",
    "user_password": "default_password",
    # Cloud / remote
    "base_url": "",
    "api_key": "",
    # Local mode
    "llm_api_key": "",
    "llm_model": "",
}


def _config_log(event: str, detail: dict | None = None) -> None:
    try:
        from datetime import datetime, timezone

        _HOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "event": event,
            **event_fields(event, "config"),
        }
        if detail:
            line["detail"] = detail
        _append_log_line(_HOOK_LOG, json.dumps(line, default=str))
    except Exception:
        pass


# Env var overrides (env var name → config key)
_ENV_MAP = {
    # Backend switch: the shared name is scanned first so the plugin-specific
    # one, applied later, wins when both are exported. The Claude Code, Codex,
    # and Qwen plugin switches are deliberately absent — an export targeting a
    # different host plugin must not flip this one.
    "COGNEE_BACKEND": "backend",
    "COGNEE_ANTIGRAVITY_BACKEND": "backend",
    "COGNEE_AGENT_NAME": "agent_name",
    "COGNEE_PLUGIN_DATASET": "dataset",
    "COGNEE_SESSION_STRATEGY": "session_strategy",
    "COGNEE_SESSION_PREFIX": "session_prefix",
    "COGNEE_BASE_URL": "base_url",
    "COGNEE_API_KEY": "api_key",
    "COGNEE_USER_EMAIL": "user_email",
    "COGNEE_USER_PASSWORD": "user_password",
    "LLM_API_KEY": "llm_api_key",
    "LLM_MODEL": "llm_model",
    # Background remember + cognify polling (read at the call sites via _float_env;
    # registered here for config-file support and discoverability).
    "COGNEE_COGNIFY_POLL_INTERVAL": "cognify_poll_interval",
    "COGNEE_REMEMBER_WAIT_SECONDS": "remember_wait_seconds",
    "COGNEE_STATUS_REQUEST_TIMEOUT": "status_request_timeout",
    # Legacy compat
    "COGNEE_SESSION_ID": "_static_session_id",
}


def load_config() -> dict:
    """Load merged config: defaults → env vars (the env file is already in os.environ)."""
    config = dict(_DEFAULTS)

    for env_key, config_key in _ENV_MAP.items():
        val = os.environ.get(env_key, "")
        if val:
            config[config_key] = val

    backend = str(config.get("backend") or "auto").lower()
    if backend in ("native", "local", "sdk"):
        config["base_url"] = ""
        config["api_key"] = ""
        config["_forced_backend"] = "local"
    elif backend in ("http", "api", "cloud", "server"):
        # Forced cloud is pinned even when connection vars are missing:
        # is_cloud_mode() honors this flag, so the plugin attempts the cloud
        # connection (and the status line reports the failure) instead of
        # silently falling back to local.
        config["_forced_backend"] = "cloud"
    else:
        # The service URL is the sole router: a URL alone is a complete
        # instruction (connect to it, or boot it if local; auth falls back to
        # the default user when no key is given). A key with no URL has nothing
        # to point at, so drop it and fall back to the local default.
        if not str(config.get("base_url") or "").strip():
            config["api_key"] = ""
            config["base_url"] = ""

    return config


def get_session_id(config: dict, cwd: Optional[str] = None) -> str:
    """Resolve the Cognee session id for this launch.

    Single-session model: the Cognee session id is minted fresh per launch and
    kept stable across the launch's separate hook processes via the host-keyed
    map (see ``resolve_cognee_session_id``). It is the single scoping key for all
    saves/recalls. The host (Antigravity) session id is read from the in-process
    ``COGNEE_SESSION_KEY`` purely as the local correlation key.

    Hooks call this after setting the host session key from their payload, so the
    resolver finds the launch's id in the map. An explicit ``COGNEE_SESSION_ID``
    env overrides, unless the launch was moved with ``switch-dataset.py``.
    """
    from _plugin_common import get_session_key, resolve_cognee_session_id

    if cwd is None:
        cwd = os.environ.get("AGY_CWD", os.getcwd())
    return resolve_cognee_session_id(get_session_key(), cwd)


def get_dataset(config: dict) -> str:
    """The dataset this launch writes to.

    Inside a launch (host session key set) the launch record is authoritative —
    it carries the dataset chosen with ``switch-dataset.py``, seeded at
    SessionStart from the env/default. Outside a launch, the config value
    (``COGNEE_PLUGIN_DATASET`` → default) applies as before.
    """
    try:
        from _plugin_common import _read_map_record, get_session_key

        host_key = get_session_key()
        if host_key:
            recorded = str(_read_map_record(host_key).get("dataset") or "").strip()
            if recorded:
                return recorded
    except Exception:
        pass
    return config.get("dataset", "agent_sessions")


def is_cloud_mode(config: dict) -> bool:
    """Check if cloud/remote mode is configured (or forced by the backend switch)."""
    return bool(config.get("base_url")) or config.get("_forced_backend") == "cloud"


def is_local_mode(config: dict) -> bool:
    """Check if local mode (has LLM key, no cloud URL)."""
    return bool(config.get("llm_api_key")) and not is_cloud_mode(config)


async def ensure_identity(config: dict):
    """Resolve the single Cognee principal for this session.

    Single-principal model: there are no per-agent users and no per-agent API
    keys. Authentication is the user-provided ``COGNEE_API_KEY`` (or a key minted
    once from the default user — handled in session-start's registration path).

    In cloud/server mode the API key already lives in the environment/cache, so
    here we only resolve the principal's user id (best-effort) for dataset
    readiness and watchers. In local SDK mode we resolve the default user.

    Returns (user_id, api_key) tuple. api_key may be empty in local mode.
    """
    service_url = config.get("base_url", "")

    if service_url:
        from _plugin_common import _api_key

        api_key = _api_key()
        user_id = await _user_id_via_api(service_url, api_key) if api_key else ""
        return user_id, api_key
    else:
        user_id = await _ensure_identity_via_sdk()
        return user_id, ""


def _cloud_http_request(
    url: str,
    *,
    method: str = "GET",
    api_key: str = "",
    json_body: dict | None = None,
    form_body: dict | None = None,
    cookies: dict | None = None,
    timeout: float = 10.0,
) -> tuple[int, str]:
    """Blocking stdlib-urllib HTTP for the cloud/remote setup path.

    Cloud mode is a thin REST client that must run without the plugin venv
    (which is only ever built in local mode), so these setup calls use urllib —
    like the runtime hot path in ``_plugin_common`` — instead of aiohttp, which
    would otherwise force the venv onto the cloud path just to be importable.

    Returns ``(status_code, body_text)``. An HTTP error status is captured as
    ``(code, body)`` rather than raised, so callers branch on the status exactly
    as they did with aiohttp; network-level errors (URLError/timeout) still
    raise, matching the aiohttp behavior the callers already guard against.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    from _plugin_common import _https_context

    headers: dict[str, str] = {}
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if api_key:
        headers["X-Api-Key"] = str(api_key).strip()
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_https_context()) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
        return exc.code, body


async def _user_id_via_api(service_url: str, api_key: str) -> str:
    """Best-effort resolve the principal's user id from an API key."""
    if not service_url or not str(api_key or "").strip():
        return ""

    base = service_url.rstrip("/")
    try:
        status, body = _cloud_http_request(f"{base}/api/v1/users/me", api_key=api_key, timeout=10.0)
        if status == 200:
            data = json.loads(body) if body else {}
            return str(data.get("id", "") or "")
    except Exception as exc:
        _config_log("users_me_lookup_failed", {"error": str(exc)[:200]})
    return ""


async def ensure_dataset_ready_via_api(service_url: str, api_key: str, dataset: str) -> None:
    """Ensure the remote backend has the dataset for the authenticated agent.

    This mirrors local SDK mode's ``ensure_dataset_ready(dataset, user)``:
    the backend creates or returns the dataset and grants permissions to
    the API-key user.
    """
    if not service_url or not api_key or not dataset:
        return

    base = service_url.rstrip("/")
    status, text = _cloud_http_request(
        f"{base}/api/v1/datasets/",  # trailing slash: cloud tenants 307-redirect the bare path
        method="POST",
        api_key=api_key,
        json_body={"name": dataset},
        timeout=30.0,
    )
    if status in (200, 201):
        return
    raise RuntimeError(f"remote dataset ensure failed ({status}: {text[:200]})")


async def _ensure_identity_via_sdk() -> str:
    """Resolve the default user via the SDK (local mode, no backend).

    Single-principal model: no agent user is created — the default user is the
    one principal that owns all sessions/data in local mode.
    """
    from cognee.modules.users.methods import get_default_user

    try:
        user = await get_default_user()
        if user:
            return str(user.id)
    except Exception as exc:
        _config_log("default_user_resolve_failed", {"error": str(exc)[:200]})
    return ""


_LOCAL_SETUP_DONE = False


async def _ensure_local_databases() -> None:
    """Create Cognee's local relational/vector stores for SDK mode."""
    global _LOCAL_SETUP_DONE
    if _LOCAL_SETUP_DONE:
        return

    from cognee.modules.engine.operations.setup import setup

    await setup()
    _LOCAL_SETUP_DONE = True


async def ensure_cognee_ready(config: dict) -> None:
    """Configure cognee for the active mode (cloud or local).

    In local SDK mode, also runs Cognee's setup() so a fresh machine or
    fresh virtualenv has its databases/tables before identity, recall, or
    session writes touch them.
    """
    if is_cloud_mode(config):
        url = config["base_url"]
        status, text = _cloud_http_request(f"{url.rstrip('/')}/health", timeout=10.0)
        if status >= 400:
            raise RuntimeError(f"backend health check failed ({status}: {text[:200]})")
        print(f"cognee-plugin: connected to {url}", file=sys.stderr)
        return

    import cognee

    if config.get("llm_api_key"):
        cognee.config.set_llm_api_key(config["llm_api_key"])
    if config.get("llm_model"):
        cognee.config.set_llm_model(config["llm_model"])

    await _ensure_local_databases()
    print("cognee-plugin: local databases ready", file=sys.stderr)


async def ensure_dataset_ready(dataset: str, user) -> None:
    """Ensure the user can write to the dataset before session bridging.

    On a fresh local install, session bridging can run before the dataset
    has been created, causing persistence to no-op with permission
    errors. Use Cognee's own pipeline resolver so dataset creation and
    ACL grants follow the SDK's normal path.

    Cognee 1.0.8's session/trace persistence pipelines call memify()
    without forwarding their user argument. In local plugin processes,
    make the resolved agent the process-local default user too, so those
    nested calls resolve the same write permissions.
    """
    from cognee.base_config import get_base_config
    from cognee.modules.pipelines.layers.resolve_authorized_user_datasets import (
        resolve_authorized_user_datasets,
    )

    email = getattr(user, "email", "")
    if email:
        get_base_config().default_user_email = email

    await resolve_authorized_user_datasets(dataset, user=user)


async def improve_session_local(
    dataset: str, session_id: str, user, *, trigger: str = "final"
) -> dict:
    """Bridge one session into the graph via the SDK's session-aware improve.

    ``cognee.improve(session_ids=[...])`` reads the session cache itself and
    runs feedback weights, QA persist, trace-feedback persist (the compact
    per-step feedback lines — not raw tool output), distillation, enrichment,
    and (in foreground mode) the graph→session sync. A cognee without it is
    reported as ``unsupported`` — there is no client-side fallback (the old
    persist path re-cognified the whole session cache on every run).
    ``trigger`` names the caller and is recorded with the session's improve
    state on success, for the idle/auto cooldown.

    Serialized per session by ``improve_session_lock``, matching the HTTP path in
    ``run_session_improve``. ``store-to-session``'s background fire takes no outer
    ``sync_lock``, so without this the local path could double-submit one session
    and have two writers contend for the single-writer graph store.
    """
    if not session_id or not user:
        return {"ok": False, "error": "missing session/user"}

    import cognee
    from _plugin_common import improve_session_lock, record_improve_success

    with improve_session_lock(session_id, "improve_session_local") as claimed:
        if not claimed:
            # Winner is already bridging this session; not dropped, just in flight.
            return {"ok": False, "skipped": "concurrent"}
        try:
            result = await cognee.improve(
                dataset,
                session_ids=[session_id],
                user=user,
                run_in_background=False,
            )
        except TypeError as exc:
            # cognee without session-aware improve. Not worked around: the old
            # persist fallback re-cognified the whole session cache every run.
            _config_log("improve_local_unsupported", {"error": str(exc)[:200]})
            return {"ok": False, "unsupported": True, "error": str(exc)[:200]}
        record_improve_success(session_id, dataset, trigger)
        return {"ok": True, "result": result}


def _get_git_branch(cwd: str) -> str:
    """Get current git branch, or empty string if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            # Sanitize for use in session IDs
            return branch.replace("/", "-").replace(" ", "-")[:40]
    except Exception as exc:
        _config_log("git_branch_lookup_failed", {"cwd": cwd, "error": str(exc)[:200]})
    return ""
