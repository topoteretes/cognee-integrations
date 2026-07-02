"""Shared configuration for the Cognee Claude Code plugin.

Loads settings from (in priority order):
  1. Environment variables (runtime overrides)
  2. Config file (~/.cognee-plugin/config.json)
  3. Defaults

Config file is created on first SessionStart if it doesn't exist.

Supports three modes:
  - Local: Cognee runs in-process (SQLite + LanceDB + Kuzu)
  - Cloud: Connect to Cognee Cloud via cognee.serve()
  - Server: Legacy — direct base_url (kept for backward compat)
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

_CONFIG_DIR = Path.home() / ".cognee-plugin" / "claude-code"
_STATE_DIR = _CONFIG_DIR
_CONFIG_FILE = _CONFIG_DIR / "config.json"
_BRIDGE_STATE_FILE = _STATE_DIR / "bridge_state.json"
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
        }
        if detail:
            line["detail"] = detail
        with _HOOK_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
    except Exception:
        pass


# Env var overrides (env var name → config key)
_ENV_MAP = {
    "COGNEE_CLAUDE_BACKEND": "backend",
    "COGNEE_CODEX_BACKEND": "backend",
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
    # Background remember + cognify polling (read at the call sites via _float_env;
    # registered here for config-file support and discoverability).
    "COGNEE_COGNIFY_POLL_INTERVAL": "cognify_poll_interval",
    "COGNEE_BRIDGE_POLL_DEADLINE": "bridge_poll_deadline",
    "COGNEE_BRIDGE_SUBMIT_TIMEOUT": "bridge_submit_timeout",
    "COGNEE_REMEMBER_WAIT_SECONDS": "remember_wait_seconds",
    "COGNEE_STATUS_REQUEST_TIMEOUT": "status_request_timeout",
    # Legacy compat
    "COGNEE_SESSION_ID": "_static_session_id",
}


# Keys a project-committed picker file may set. Deliberately excludes secrets
# and backend routing (api_key, llm_api_key, base_url, backend, user_*) so that
# merely opening a repo with a `.cognee/session-config.json` can never redirect
# your Cognee backend or inject credentials — the picker's job is dataset/session
# selection only (issue #3686: "the dataset configuration key").
_PICKER_ALLOWED_KEYS = frozenset(
    {"dataset", "session_strategy", "session_prefix", "agent_name", "top_k"}
)


def _read_project_picker(cwd: Optional[str] = None) -> dict:
    """Read .cognee/session-config.json from the project directory.

    Resolution order for the project root: explicit cwd arg (from a hook's
    payload) > CLAUDE_CWD env (background workers with no payload) >
    os.getcwd() (last resort — NOT reliable inside a global-plugin hook
    process, kept only as a final fallback).

    Only the non-sensitive keys in ``_PICKER_ALLOWED_KEYS`` are honored; any
    other key in the file is ignored so an untrusted repo file cannot influence
    auth or backend routing.
    """
    project_dir = Path(cwd or os.environ.get("CLAUDE_CWD") or os.getcwd())
    picker_path = project_dir / ".cognee" / "session-config.json"

    if not picker_path.exists():
        return {}
    try:
        data = json.loads(picker_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _config_log(
            "session_picker_load_failed", {"path": str(picker_path), "error": str(exc)[:200]}
        )
        return {}
    if not isinstance(data, dict):
        return {}
    # Only allowlisted, non-null AND non-empty-string values fall through — the
    # empty/null check matches the config-file layer's semantics (line 112:
    # `v is not None and v != ""`) so an explicit `{"dataset": null}` or
    # `{"dataset": ""}` cleanly no-ops instead of resolving to an empty name.
    ignored = [k for k in data if k not in _PICKER_ALLOWED_KEYS]
    if ignored:
        _config_log("session_picker_ignored_keys", {"keys": sorted(ignored)[:20]})
    return {
        k: v
        for k, v in data.items()
        if k in _PICKER_ALLOWED_KEYS and v is not None and v != ""
    }


def load_config(cwd: Optional[str] = None) -> dict:
    """Load merged config: defaults → file → project picker → env vars."""
    config = dict(_DEFAULTS)

    # Layer 2: config file
    if _CONFIG_FILE.exists():
        try:
            file_cfg = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            config.update({k: v for k, v in file_cfg.items() if v is not None and v != ""})
        except Exception as exc:
            _config_log(
                "config_file_load_failed", {"path": str(_CONFIG_FILE), "error": str(exc)[:200]}
            )

    # Layer 3: project-level picker (.cognee/session-config.json)
    config.update(_read_project_picker(cwd))

    # Layer 4: env vars (highest priority)
    for env_key, config_key in _ENV_MAP.items():
        val = os.environ.get(env_key, "")
        if val:
            config[config_key] = val

    backend = str(config.get("backend") or "auto").lower()
    if backend in ("native", "local", "sdk"):
        config["base_url"] = ""
        config["api_key"] = ""
        config["base_url"] = ""
    elif backend not in ("http", "api", "cloud", "server"):
        # The service URL is the sole router: a URL alone is a complete
        # instruction (connect to it, or boot it if local; auth falls back to
        # the default user when no key is given). A key with no URL has nothing
        # to point at, so drop it and fall back to the local default.
        if not str(config.get("base_url") or "").strip():
            config["api_key"] = ""
            config["base_url"] = ""

    return config


def save_config(config: dict) -> None:
    """Write config to disk. Creates directory if needed."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Only save non-secret, non-default values
    transient_keys = {"api_key", "llm_api_key", "base_url", "backend"}
    to_save = {
        k: v
        for k, v in config.items()
        if k not in transient_keys and not k.startswith("_") and v and v != _DEFAULTS.get(k)
    }
    _CONFIG_FILE.write_text(json.dumps(to_save, indent=2), encoding="utf-8")


def get_session_id(config: dict, cwd: Optional[str] = None) -> str:
    """Resolve the Cognee session id for this launch.

    Single-session model: the Cognee session id is minted fresh per launch and
    kept stable across the launch's separate hook processes via the host-keyed
    map (see ``resolve_cognee_session_id``). It is the single scoping key for all
    saves/recalls. The host (Claude) session id is read from the in-process
    ``COGNEE_SESSION_KEY`` purely as the local correlation key.

    Hooks call this after setting the host session key from their payload, so the
    resolver finds the launch's id in the map. An explicit ``COGNEE_SESSION_ID``
    env (or ``.cognee/session-config.json`` picker) overrides.
    """
    from _plugin_common import get_session_key, resolve_cognee_session_id

    if cwd is None:
        cwd = os.environ.get("CLAUDE_CWD", os.getcwd())
    return resolve_cognee_session_id(get_session_key(), cwd)


def get_dataset(config: dict) -> str:
    """Get the dataset name from config."""
    return config.get("dataset", "agent_sessions")


def is_cloud_mode(config: dict) -> bool:
    """Check if cloud/remote mode is configured."""
    return bool(config.get("base_url"))


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


async def _user_id_via_api(service_url: str, api_key: str) -> str:
    """Best-effort resolve the principal's user id from an API key."""
    if not service_url or not str(api_key or "").strip():
        return ""

    import aiohttp

    base = service_url.rstrip("/")
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"X-Api-Key": str(api_key).strip()}
        ) as session:
            async with session.get(f"{base}/api/v1/users/me") as resp:
                if resp.status == 200:
                    data = await resp.json()
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

    import aiohttp

    base = service_url.rstrip("/")
    async with aiohttp.ClientSession(headers={"X-Api-Key": api_key}) as session:
        async with session.post(f"{base}/api/v1/datasets", json={"name": dataset}) as resp:
            if resp.status in (200, 201):
                return
            text = await resp.text()
            raise RuntimeError(f"remote dataset ensure failed ({resp.status}: {text[:200]})")


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
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(f"{url.rstrip('/')}/health") as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise RuntimeError(f"backend health check failed ({resp.status}: {text[:200]})")
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


async def sync_graph_context_to_session(dataset: str, session_id: str, user) -> dict:
    """Sync permanent graph context into one session without full improve().

    ``cognee.improve(session_ids=[...])`` also persists session cache
    entries. The integration already does that explicitly via
    ``persist_session_cache_to_graph()``, so call only Cognee's final
    graph-context sync step to keep recall working without duplicating
    session documents or holding the DB lock for the full improve path.
    """
    if not session_id or not user:
        return {"synced": 0}

    from cognee.modules.pipelines.layers.resolve_authorized_user_datasets import (
        resolve_authorized_user_datasets,
    )
    from cognee.tasks.memify.sync_graph_to_session import sync_graph_to_session

    _, authorized_datasets = await resolve_authorized_user_datasets(dataset, user=user)
    if not authorized_datasets:
        return {"synced": 0}

    return await sync_graph_to_session(
        user_id=str(user.id),
        session_id=session_id,
        dataset_id=authorized_datasets[0].id,
        dataset_name=dataset,
    )


def _read_field(entry, field: str) -> str:
    if isinstance(entry, dict):
        return str(entry.get(field) or "")
    return str(getattr(entry, field, "") or "")


def _load_bridge_state() -> dict:
    try:
        return json.loads(_BRIDGE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        _config_log(
            "bridge_state_load_failed", {"path": str(_BRIDGE_STATE_FILE), "error": str(exc)[:200]}
        )
        return {}


def _save_bridge_state(state: dict) -> None:
    try:
        _BRIDGE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _BRIDGE_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        _config_log(
            "bridge_state_save_failed", {"path": str(_BRIDGE_STATE_FILE), "error": str(exc)[:200]}
        )


def _bridge_state_key(dataset: str, session_id: str, user_id: str, kind: str) -> str:
    return hashlib.sha256(f"{user_id}:{dataset}:{session_id}:{kind}".encode()).hexdigest()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def persist_session_cache_to_graph(dataset: str, session_id: str, user) -> bool:
    """Persist cached session QA/trace text to the permanent graph.

    This is a local-mode compatibility bridge for Cognee 1.0.8. The SDK's
    built-in session persistence can complete without extracting entries
    from the file-system cache in the plugin setup. Reading the same
    cache directly here keeps the integration useful while still using
    cognee.remember() for the actual add+cognify pipeline.
    """
    if not session_id or not user:
        return False

    import cognee
    from cognee.infrastructure.session.get_session_manager import get_session_manager

    await ensure_dataset_ready(dataset, user)

    user_id = str(user.id)
    session_manager = get_session_manager()
    if not session_manager.is_available:
        return False

    wrote = False
    bridge_state = _load_bridge_state()
    state_changed = False

    qa_entries = await session_manager.get_session(
        user_id=user_id,
        session_id=session_id,
        formatted=False,
    )
    qa_lines: list[str] = []
    for entry in qa_entries or []:
        question = _read_field(entry, "question").strip()
        answer = _read_field(entry, "answer").strip()
        if question:
            qa_lines.append(f"Question: {question}")
        if answer:
            qa_lines.append(f"Answer: {answer}")
        if question or answer:
            qa_lines.append("")
    qa_text = "\n".join(qa_lines).strip()
    if qa_text:
        qa_document = f"Session ID: {session_id}\n\n{qa_text}"
        qa_key = _bridge_state_key(dataset, session_id, user_id, "qa")
        qa_hash = _content_hash(qa_document)
        if bridge_state.get(qa_key) == qa_hash:
            qa_text = ""
        else:
            bridge_state[qa_key] = qa_hash
            state_changed = True

    if qa_text:
        await cognee.remember(
            qa_document,
            dataset_name=dataset,
            node_set=["user_sessions_from_cache"],
            self_improvement=False,
            run_in_background=False,
            user=user,
        )
        wrote = True

    trace_values = await session_manager.get_agent_trace_feedback(
        user_id=user_id,
        session_id=session_id,
    )
    trace_text = "\n".join(str(value).strip() for value in trace_values or [] if str(value).strip())
    if trace_text:
        trace_document = f"Session ID: {session_id}\n\n{trace_text}"
        trace_key = _bridge_state_key(dataset, session_id, user_id, "trace")
        trace_hash = _content_hash(trace_document)
        if bridge_state.get(trace_key) == trace_hash:
            trace_text = ""
        else:
            bridge_state[trace_key] = trace_hash
            state_changed = True

    if trace_text:
        await cognee.remember(
            trace_document,
            dataset_name=dataset,
            node_set=["agent_trace_feedbacks"],
            self_improvement=False,
            run_in_background=False,
            user=user,
        )
        wrote = True

    if state_changed:
        _save_bridge_state(bridge_state)

    return wrote


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
