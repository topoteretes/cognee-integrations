"""Shared configuration for the Cognee Claude Code plugin.

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
COGNEE_CLAUDE_BACKEND does the same for this plugin only, beating the shared
name. A forced mode is pinned: forced local scrubs the cloud connection vars
from the process environment (see _env_file), and forced cloud keeps
is_cloud_mode() true even when connection vars are missing, so the plugin
attempts the cloud connection and the status line reports what is wrong
instead of silently falling back to local.

Two modes, both over HTTP — the hooks never import cognee in-process:
  - Local: the plugin boots a Cognee server on localhost and talks to it
  - Cloud: connect to a remote Cognee server via COGNEE_BASE_URL + COGNEE_API_KEY
"""

from __future__ import annotations

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

_STATE_DIR = Path.home() / ".cognee-plugin" / "claude-code"
_HOOK_LOG = _STATE_DIR / "hook.log"

_DEFAULTS = {
    "dataset": "agent_sessions",
    "agent_name": "claude-code-agent",
    "session_strategy": "per-directory",  # per-directory | git-branch | static
    "session_prefix": "claude",  # agent name; session id is "{agent}_{host_session_id}"
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
    # Memory steering: assert Cognee as the preferred memory over Claude Code's
    # built-in auto memory (MEMORY.md). Opt out with COGNEE_PREFER_MEMORY=false.
    "prefer_cognee_memory": True,
    # Plugin identity: a dedicated agent sub-user + API key for this plugin
    # (POST /api/v1/integrations/plugins/claude-code/provision) so cognee attributes
    # its traffic per plugin. "auto" (default) provisions one only in service of
    # shared agent memory (below) and falls back to the principal when that
    # cannot be wired; "true" requires an identity and never falls back;
    # "false" runs as the principal and ignores a cached identity.
    "plugin_identity": "auto",
    # Shared agent memory: every plugin agent of this user joins one shared role
    # (``cognee-agent``) with read+write on the user's datasets, and the launch's
    # dataset is addressed by its canonical UUID — so each plugin recalls what
    # the others stored. Under identity mode ``auto`` this is what provisions an
    # agent in the first place. Opt out with COGNEE_SHARED_AGENT_MEMORY=false
    # for separated, per-plugin memory.
    "shared_agent_memory": True,
    # Background remember + cognify status polling. Remember runs in the background
    # (so a large cognify never holds one request open past the cloud's ~10-min
    # request ceiling); these tune how completion is polled afterwards.
    "cognify_poll_interval": 3.0,  # seconds between status polls
    "bridge_poll_deadline": 600.0,  # session->graph bridge: overall wait for COMPLETED
    "bridge_submit_timeout": 30.0,  # the background POST read timeout (enqueue is fast)
    "remember_wait_seconds": 8.0,  # explicit "remember this": bounded wait, 0 disables
    "status_request_timeout": 10.0,  # per-poll GET timeout
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
    # one, applied later, wins when both are exported. COGNEE_CODEX_BACKEND is
    # deliberately absent — an export targeting the Codex plugin must not flip
    # this one.
    "COGNEE_BACKEND": "backend",
    "COGNEE_CLAUDE_BACKEND": "backend",
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
    "COGNEE_PREFER_MEMORY": "prefer_cognee_memory",
    "COGNEE_PLUGIN_IDENTITY": "plugin_identity",
    "COGNEE_SHARED_AGENT_MEMORY": "shared_agent_memory",
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
    saves/recalls. The host (Claude) session id is read from the in-process
    ``COGNEE_SESSION_KEY`` purely as the local correlation key.

    Hooks call this after setting the host session key from their payload, so the
    resolver finds the launch's id in the map. An explicit ``COGNEE_SESSION_ID``
    env overrides, unless the launch was moved with ``switch-dataset.py``.
    """
    from _plugin_common import get_session_key, resolve_cognee_session_id

    if cwd is None:
        cwd = os.environ.get("CLAUDE_CWD", os.getcwd())
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

    from _plugin_common import _https_context, urlopen_following_307

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
        with urlopen_following_307(req, timeout=timeout, context=_https_context()) as resp:
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
    """Ensure the backend has the dataset for the authenticated agent.

    The backend creates or returns the dataset and grants permissions to the
    API-key user.
    """
    if not service_url or not api_key or not dataset:
        return

    base = service_url.rstrip("/")
    from _dataset_access import dataset_id

    ident = dataset_id(dataset)
    if ident:
        from _plugin_common import require_typed_dataset_id_support

        require_typed_dataset_id_support(service_url=service_url, api_key=api_key)
        user_id = await _user_id_via_api(service_url, api_key)
        if not user_id:
            raise RuntimeError("Cannot authorize dataset ID without authenticated identity")
        status, text = _cloud_http_request(
            f"{base}/api/v1/permissions/principals/{user_id}/datasets?permission_name=write",
            api_key=api_key,
            timeout=15.0,
        )
        if status != 200 or not any(str(row.get("id")) == ident for row in json.loads(text)):
            raise RuntimeError("403: no verified write permission on selected dataset")
        return
    status, text = _cloud_http_request(
        # Either spelling works: _cloud_http_request replays a same-origin
        # 307/308, which is how cloud (bare -> slashed) and local
        # (slashed -> bare) servers disagree about this route.
        f"{base}/api/v1/datasets/",
        method="POST",
        api_key=api_key,
        json_body={"name": dataset},
        timeout=30.0,
    )
    if status in (200, 201):
        return
    raise RuntimeError(f"remote dataset ensure failed ({status}: {text[:200]})")


async def ensure_cognee_ready(config: dict) -> None:
    """Confirm the configured server answers ``/health``.

    Raises on an HTTP error status so callers can classify the connection
    state. There is no in-process fallback: the hooks are HTTP clients only.
    """
    url = str(config.get("base_url") or "").strip()
    if not url:
        raise RuntimeError("no Cognee server URL configured")
    status, text = _cloud_http_request(f"{url.rstrip('/')}/health", timeout=10.0)
    if status >= 400:
        raise RuntimeError(f"backend health check failed ({status}: {text[:200]})")
    print(f"cognee-plugin: connected to {url}", file=sys.stderr)


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
