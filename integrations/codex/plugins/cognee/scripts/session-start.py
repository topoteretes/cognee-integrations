#!/usr/bin/env python3
"""Initialize Cognee memory at session start.

Runs on the SessionStart hook. Responsibilities:
  1. Load config (file + env vars)
  2. Compute per-directory session ID
  3. Connect to Cognee Cloud if configured
  4. Configure local LLM if local mode
  5. Register the current Codex thread as an active agent connection
"""

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Add scripts dir to path for config import
sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (
    hook_log,
    mark_server_ready,
    quiet_hook_output,
    resolve_session_key_from_payload,
    server_health_ok,
    set_session_key,
    touch_activity,
)
from config import (
    ensure_cognee_ready,
    ensure_dataset_ready,
    ensure_dataset_ready_via_api,
    ensure_identity,
    get_dataset,
    get_session_id,
    is_cloud_mode,
    load_config,
    save_config,
)

_STATE_DIR = Path.home() / ".cognee-plugin" / "codex"
_GLOBAL_STATE_DIR = Path.home() / ".cognee-plugin"
_WATCHER_PID = _STATE_DIR / "watcher.pid"
_WATCHER_STOP = _STATE_DIR / "watcher.stop"
_WATCHER_SCRIPT = Path(__file__).with_name("idle-watcher.py")
_EXIT_WATCHER_SCRIPT = Path(__file__).with_name("exit-watcher.py")
_EXIT_WATCHERS_DIR = _STATE_DIR / "exit-watchers"
_AGENT_KEYS_CACHE = _STATE_DIR / "agent_keys.json"
_AGENT_KEYS_LOCK = _STATE_DIR / "agent_keys.lock"
_AGENT_KEYS_LOCK_STALE_SECONDS = 120
_AGENT_KEYS_LOCK_WAIT_SECONDS = 4.0
_AGENT_KEYS_LOCK_POLL_SECONDS = 0.05
_AGENT_LIFECYCLE_LOCKS_DIR = _STATE_DIR / "agent_lifecycle_locks"
_AGENT_LIFECYCLE_LOCK_STALE_SECONDS = 120
_AGENT_LIFECYCLE_LOCK_WAIT_SECONDS = 20.0
_AGENT_LIFECYCLE_LOCK_POLL_SECONDS = 0.05
_LOCAL_SERVICE_URL = "http://localhost:8011"
_HEALTH_URL = f"{_LOCAL_SERVICE_URL}/health"
_HEALTH_TIMEOUT_SECONDS = 30
_HEALTH_POLL_SECONDS = 1.0
_SERVER_BOOT_LOCK = _GLOBAL_STATE_DIR / "server-bootstrap.lock"
_SERVER_BOOT_LOCK_STALE_SECONDS = 2 * _HEALTH_TIMEOUT_SECONDS
_SERVER_BOOT_LOCK_WAIT_SECONDS = _HEALTH_TIMEOUT_SECONDS + 5.0
_SERVER_BOOT_LOCK_POLL_SECONDS = 0.2

# Lazy bootstrap: defer server boot + registration to a detached worker so the
# SessionStart hook returns fast and migrations never time out the 15s budget.
_BOOTSTRAP_ARG = "--bootstrap"
_BOOTSTRAP_LOCK = _GLOBAL_STATE_DIR / "server-bootstrap-worker.lock"
_SERVER_BOOT_DEADLINE_SECONDS = float(os.environ.get("COGNEE_SERVER_BOOT_DEADLINE", "") or 600.0)
_BOOTSTRAP_LOCK_STALE_SECONDS = _SERVER_BOOT_DEADLINE_SECONDS + 60.0
_LAZY_BOOTSTRAP = os.environ.get("COGNEE_LAZY_BOOTSTRAP", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)


def _health_ok(url: str = _HEALTH_URL, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _wait_for_health(deadline_seconds: float) -> bool:
    """Poll /health until the server is serving or the deadline elapses.

    Used by bootstrap workers that did not win the boot single-flight: they
    don't spawn uvicorn, they just wait for whichever worker did to finish
    booting (including migrations) before registering.
    """
    deadline = time.monotonic() + deadline_seconds
    while True:
        if _health_ok():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_HEALTH_POLL_SECONDS)


def _ensure_local_server_running(
    config: dict, health_timeout: float = _HEALTH_TIMEOUT_SECONDS
) -> None:
    if _health_ok():
        config["service_url"] = _LOCAL_SERVICE_URL
        os.environ["COGNEE_SERVICE_URL"] = _LOCAL_SERVICE_URL
        return

    owner = f"session-start:{os.getpid()}"
    acquired = False
    deadline = time.monotonic() + _SERVER_BOOT_LOCK_WAIT_SECONDS
    try:
        _SERVER_BOOT_LOCK.parent.mkdir(parents=True, exist_ok=True)
        while True:
            now = time.time()
            if _SERVER_BOOT_LOCK.exists():
                stale = False
                try:
                    raw = json.loads(_SERVER_BOOT_LOCK.read_text(encoding="utf-8"))
                    pid = int(raw.get("pid", 0) or 0)
                    created_at = float(raw.get("created_at", 0) or 0)
                    stale = (not _pid_alive(pid)) or (
                        now - created_at > _SERVER_BOOT_LOCK_STALE_SECONDS
                    )
                except Exception:
                    stale = True
                if stale:
                    try:
                        _SERVER_BOOT_LOCK.unlink()
                        hook_log("server_bootstrap_lock_stale_reaped", {"owner": owner})
                    except Exception as exc:
                        hook_log("server_bootstrap_lock_unlink_failed", {"error": str(exc)[:200]})

            try:
                fd = os.open(str(_SERVER_BOOT_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"owner": owner, "pid": os.getpid(), "created_at": now}, fh)
                acquired = True
                hook_log("server_bootstrap_lock_acquired", {"owner": owner})
                break
            except FileExistsError:
                if _health_ok():
                    config["service_url"] = _LOCAL_SERVICE_URL
                    os.environ["COGNEE_SERVICE_URL"] = _LOCAL_SERVICE_URL
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError("server bootstrap lock timeout")
                time.sleep(_SERVER_BOOT_LOCK_POLL_SECONDS)

        if _health_ok():
            config["service_url"] = _LOCAL_SERVICE_URL
            os.environ["COGNEE_SERVICE_URL"] = _LOCAL_SERVICE_URL
            return

        server_env = os.environ.copy()
        subprocess.Popen(
            ["uvicorn", "cognee.api.client:app", "--port", "8011"],
            env=server_env,
            start_new_session=True,
        )

        health_deadline = time.monotonic() + health_timeout
        while time.monotonic() < health_deadline:
            if _health_ok():
                config["service_url"] = _LOCAL_SERVICE_URL
                os.environ["COGNEE_SERVICE_URL"] = _LOCAL_SERVICE_URL
                return
            time.sleep(_HEALTH_POLL_SECONDS)

        raise RuntimeError(
            f"Cognee server did not become healthy at {_HEALTH_URL} within {health_timeout}s"
        )
    finally:
        if acquired:
            try:
                _SERVER_BOOT_LOCK.unlink()
                hook_log("server_bootstrap_lock_released", {"owner": owner})
            except Exception as exc:
                hook_log("server_bootstrap_lock_release_failed", {"error": str(exc)[:200]})


def _load_agent_keys_cache() -> dict:
    empty = {"version": 1, "entries": {}}
    try:
        if _AGENT_KEYS_CACHE.exists():
            data = json.loads(_AGENT_KEYS_CACHE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                return data
    except Exception as exc:
        hook_log("agent_keys_cache_load_failed", {"error": str(exc)[:200]})
    return empty


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


@contextmanager
def _agent_keys_lock(owner: str):
    acquired = False
    deadline = time.monotonic() + _AGENT_KEYS_LOCK_WAIT_SECONDS
    try:
        _AGENT_KEYS_LOCK.parent.mkdir(parents=True, exist_ok=True)
        while True:
            now = time.time()
            if _AGENT_KEYS_LOCK.exists():
                stale = False
                try:
                    raw = json.loads(_AGENT_KEYS_LOCK.read_text(encoding="utf-8"))
                    pid = int(raw.get("pid", 0) or 0)
                    created_at = float(raw.get("created_at", 0) or 0)
                    stale = (not _pid_alive(pid)) or (
                        now - created_at > _AGENT_KEYS_LOCK_STALE_SECONDS
                    )
                except Exception:
                    stale = True
                if stale:
                    try:
                        _AGENT_KEYS_LOCK.unlink()
                    except Exception as exc:
                        hook_log("agent_keys_lock_unlink_failed", {"error": str(exc)[:200]})

            try:
                fd = os.open(str(_AGENT_KEYS_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"owner": owner, "pid": os.getpid(), "created_at": now}, fh)
                acquired = True
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("agent keys lock timeout")
                time.sleep(_AGENT_KEYS_LOCK_POLL_SECONDS)

        yield
    finally:
        if acquired:
            try:
                _AGENT_KEYS_LOCK.unlink()
            except Exception as exc:
                hook_log("agent_keys_lock_release_failed", {"error": str(exc)[:200]})


def _save_agent_keys_cache(data: dict) -> None:
    try:
        _AGENT_KEYS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _AGENT_KEYS_CACHE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, _AGENT_KEYS_CACHE)
    except Exception as exc:
        hook_log("agent_keys_cache_save_failed", {"error": str(exc)[:200]})


def _normalize_service_url(service_url: str) -> str:
    return str(service_url or "").strip().rstrip("/")


def _agent_cache_key(service_url: str, agent_name: str) -> str:
    return f"{_normalize_service_url(service_url)}::{agent_name}"


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _agent_lifecycle_lock_path(service_url: str, agent_name: str) -> Path:
    lock_key = _agent_cache_key(service_url, agent_name)
    digest = hashlib.sha256(lock_key.encode("utf-8")).hexdigest()[:32]
    return _AGENT_LIFECYCLE_LOCKS_DIR / f"{digest}.lock"


@contextmanager
def _agent_lifecycle_lock(service_url: str, agent_name: str, owner: str):
    acquired = False
    lock_path = _agent_lifecycle_lock_path(service_url, agent_name)
    deadline = time.monotonic() + _AGENT_LIFECYCLE_LOCK_WAIT_SECONDS
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            now = time.time()
            if lock_path.exists():
                stale = False
                try:
                    raw = json.loads(lock_path.read_text(encoding="utf-8"))
                    pid = int(raw.get("pid", 0) or 0)
                    created_at = float(raw.get("created_at", 0) or 0)
                    stale = (not _pid_alive(pid)) or (
                        now - created_at > _AGENT_LIFECYCLE_LOCK_STALE_SECONDS
                    )
                except Exception:
                    stale = True
                if stale:
                    try:
                        lock_path.unlink()
                    except Exception as exc:
                        hook_log("agent_lifecycle_lock_unlink_failed", {"error": str(exc)[:200]})

            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"owner": owner, "pid": os.getpid(), "created_at": now}, fh)
                acquired = True
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("agent lifecycle lock timeout")
                time.sleep(_AGENT_LIFECYCLE_LOCK_POLL_SECONDS)
        yield
    finally:
        if acquired:
            try:
                lock_path.unlink()
            except Exception as exc:
                hook_log("agent_lifecycle_lock_release_failed", {"error": str(exc)[:200]})


async def _login_default_user_for_owner_api_key(service_url: str, config: dict) -> str:
    import aiohttp

    base = _normalize_service_url(service_url)
    email = config.get("user_email", "")
    password = config.get("user_password", "")

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{base}/api/v1/auth/login",
            data={"username": email, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    "default-user login failed "
                    f"({resp.status}: {body[:200]}). "
                    "Set COGNEE_USER_EMAIL/COGNEE_USER_PASSWORD correctly."
                )
            login_data = await resp.json()
            jwt = str(login_data.get("access_token", "") or "")

        if not jwt:
            raise RuntimeError("default-user login returned no access token")

        async with session.get(
            f"{base}/api/v1/auth/api-keys",
            cookies={"auth_token": jwt},
        ) as resp:
            if resp.status == 200:
                keys = await resp.json()
                if isinstance(keys, list) and keys:
                    key = str(keys[0].get("key", "") or "")
                    if key:
                        return key

        async with session.post(
            f"{base}/api/v1/auth/api-keys",
            json={"name": "codex-owner-bootstrap"},
            cookies={"auth_token": jwt},
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"default-user API key creation failed ({resp.status}: {body[:200]})"
                )
            payload = await resp.json()
            key = str(payload.get("key", "") or "")
            if not key:
                raise RuntimeError("default-user API key creation returned empty key")
            return key


def _resolve_agent_name(config: dict, cwd: str) -> str:
    def _normalize(name: str) -> str:
        raw = str(name or "").strip()
        if raw.endswith("@cognee.agent"):
            raw = raw[: -len("@cognee.agent")]
        suffix = "_codex"
        if raw.endswith(suffix):
            return raw
        return f"{raw}{suffix}"

    configured = str(config.get("agent_name", "") or "").strip()
    if configured:
        return _normalize(configured)
    return _normalize(f"codex-{Path(cwd).name}")


async def _create_agent_with_bootstrap_key(
    service_url: str,
    agent_name: str,
    bootstrap_key: str,
) -> tuple[str, str]:
    import aiohttp

    async def _delete_agent_by_name(
        session: aiohttp.ClientSession, base_url: str, name: str
    ) -> bool:
        async with session.get(f"{base_url}/api/v1/agents/list") as list_resp:
            if list_resp.status != 200:
                body = await list_resp.text()
                raise RuntimeError(f"list agents failed ({list_resp.status}: {body[:200]})")
            agents = await list_resp.json()

        target_id = ""
        for item in agents if isinstance(agents, list) else []:
            if not isinstance(item, dict):
                continue
            email = str(item.get("agentEmail", "") or "").strip()
            short_name = email[:-13] if email.endswith("@cognee.agent") else email
            if short_name == name:
                target_id = str(item.get("agentId", "") or "").strip()
                break

        if not target_id:
            return False

        async with session.delete(f"{base_url}/api/v1/agents/{target_id}") as del_resp:
            if del_resp.status not in (200, 204):
                body = await del_resp.text()
                raise RuntimeError(f"delete agent failed ({del_resp.status}: {body[:200]})")
        return True

    def _parse_create_payload(payload: dict) -> tuple[str, str]:
        return (
            str(payload.get("agentId", "") or ""),
            str(payload.get("agentApiKey", "") or ""),
        )

    headers = {"Content-Type": "application/json"}
    if bootstrap_key:
        headers["X-Api-Key"] = bootstrap_key

    base = service_url.rstrip("/")
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(
            f"{base}/api/v1/agents/create", params={"name": agent_name}
        ) as resp:
            if resp.status == 200:
                payload = await resp.json()
                return _parse_create_payload(payload)
            if resp.status == 409:
                deleted = await _delete_agent_by_name(session, base, agent_name)
                if not deleted:
                    raise RuntimeError(
                        f"Agent '{agent_name}' already exists on {base}, "
                        "but it could not be resolved for deletion."
                    )
                async with session.post(
                    f"{base}/api/v1/agents/create", params={"name": agent_name}
                ) as retry_resp:
                    if retry_resp.status == 200:
                        payload = await retry_resp.json()
                        return _parse_create_payload(payload)
                    text = await retry_resp.text()
                    raise RuntimeError(
                        f"create_agent retry failed ({retry_resp.status}: {text[:200]})"
                    )
            text = await resp.text()
            raise RuntimeError(f"create_agent failed ({resp.status}: {text[:200]})")


async def _agent_api_key_is_valid(service_url: str, api_key: str) -> bool:
    import aiohttp

    base = _normalize_service_url(service_url)
    if not base or not str(api_key or "").strip():
        return False
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"X-Api-Key": str(api_key).strip()},
        ) as session:
            async with session.get(f"{base}/api/v1/users/me") as resp:
                return resp.status == 200
    except Exception as exc:
        hook_log("agent_key_validation_failed", {"error": str(exc)[:200]})
        return False


async def _ensure_agent_credentials_and_register(
    config: dict, cwd: str, session_id: str, agent_session_name: str, session_key: str
) -> tuple[str, str, str, bool]:
    service_url = _normalize_service_url(str(config.get("service_url", "") or ""))
    if not service_url:
        return "", "", "", False

    agent_name = _resolve_agent_name(config, cwd)
    cache_key = _agent_cache_key(service_url, agent_name)
    agent_id = ""
    agent_api_key = ""
    registered = False
    registration: dict = {}
    lifecycle_owner = f"session-start:lifecycle:{os.getpid()}"
    with _agent_lifecycle_lock(service_url, agent_name, lifecycle_owner):
        with _agent_keys_lock("session-start:read"):
            cache = _load_agent_keys_cache()
            entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
            cached = entries.get(cache_key, {}) if isinstance(entries, dict) else {}
            agent_id = str(cached.get("agent_id", "") or "")
            agent_api_key = str(cached.get("api_key", "") or "")
            if agent_api_key:
                cached["last_used_at"] = _utc_iso_now()
                entries[cache_key] = cached
                cache["entries"] = entries
                _save_agent_keys_cache(cache)

        if agent_api_key:
            key_valid = await _agent_api_key_is_valid(service_url, agent_api_key)
            if not key_valid:
                hook_log(
                    "agent_key_stale_detected",
                    {"agent_name": agent_name, "service_url": service_url},
                )
                agent_id = ""
                agent_api_key = ""
                with _agent_keys_lock("session-start:invalidate-stale"):
                    cache = _load_agent_keys_cache()
                    entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
                    if isinstance(entries, dict):
                        entries.pop(cache_key, None)
                        cache["entries"] = entries
                        _save_agent_keys_cache(cache)
            else:
                with _agent_keys_lock("session-start:touch-last-used"):
                    cache = _load_agent_keys_cache()
                    entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
                    latest = entries.get(cache_key, {}) if isinstance(entries, dict) else {}
                    if isinstance(latest, dict):
                        latest["last_used_at"] = _utc_iso_now()
                        entries[cache_key] = latest
                        cache["entries"] = entries
                        _save_agent_keys_cache(cache)

        created_agent_id = ""
        created_key = ""
        if not agent_api_key:
            bootstrap_key = str(
                config.get("api_key", "") or os.environ.get("COGNEE_API_KEY", "")
            ).strip()
            if not bootstrap_key:
                bootstrap_key = await _login_default_user_for_owner_api_key(service_url, config)
            created_agent_id, created_key = await _create_agent_with_bootstrap_key(
                service_url, agent_name, bootstrap_key
            )
            if created_key:
                agent_id = created_agent_id
                agent_api_key = created_key
                with _agent_keys_lock("session-start:write"):
                    cache = _load_agent_keys_cache()
                    entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
                    latest = entries.get(cache_key, {}) if isinstance(entries, dict) else {}
                    latest_key = str(latest.get("api_key", "") or "")
                    if latest_key:
                        agent_id = str(latest.get("agent_id", "") or "") or agent_id
                        agent_api_key = latest_key
                        latest["last_used_at"] = _utc_iso_now()
                        entries[cache_key] = latest
                    else:
                        entries[cache_key] = {
                            "agent_id": agent_id,
                            "agent_name": agent_name,
                            "api_key": agent_api_key,
                            "service_url": service_url,
                            "created_at": _utc_iso_now(),
                            "last_used_at": _utc_iso_now(),
                        }
                    cache["entries"] = entries
                    _save_agent_keys_cache(cache)

        if not agent_api_key:
            return "", "", agent_name, False

        os.environ["COGNEE_API_KEY"] = agent_api_key
        config["api_key"] = agent_api_key

        from _plugin_common import register_agent_via_http

        registered, registration = register_agent_via_http(
            agent_session_name=agent_session_name,
            session_id=session_id,
            dataset_names=[str(config.get("dataset", "") or "").strip()],
        )
        if not registered:
            raise RuntimeError(
                f"Failed to register agent '{agent_name}' on {service_url}. "
                "Cached key may be invalid. Delete and recreate the agent."
            )
    hook_log(
        "agent_register_result",
        {
            "agent_name": agent_name,
            "agent_id": agent_id,
            "agent_session_name": agent_session_name,
            "registered": registered,
            "connection_id": str(registration.get("id", "")),
            "session_id": session_id,
        },
    )

    return agent_id, agent_api_key, agent_name, registered


def _watcher_alive() -> bool:
    if not _WATCHER_PID.exists():
        return False
    try:
        pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _spawn_idle_watcher(
    session_id: str, dataset: str, user_id: str, config: dict, session_key: str
) -> None:
    """Launch the idle watcher as a detached background process.

    Idempotent: if a watcher is already alive (from an earlier session
    on the same machine), we kill it so the new one picks up the new
    session. Launched with its own session via ``start_new_session=True``
    so it survives the parent shell closing.
    """
    if _watcher_alive():
        try:
            pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip())
            os.kill(pid, signal.SIGTERM)
        except Exception as exc:
            hook_log("idle_watcher_kill_failed", {"error": str(exc)[:200]})

    # Clear any stale stop sentinel from a previous run.
    try:
        if _WATCHER_STOP.exists():
            _WATCHER_STOP.unlink()
    except Exception as exc:
        hook_log("watcher_stop_unlink_failed", {"error": str(exc)[:200]})

    # Only the non-secret surface of config needs to travel — the
    # watcher re-runs ``ensure_cognee_ready`` on its own.
    bootstrap = {
        "session_id": session_id,
        "dataset": dataset,
        "user_id": user_id,
        "session_key": session_key,
        "config": {
            "service_url": config.get("service_url", ""),
            "llm_model": config.get("llm_model", ""),
            "dataset": dataset,
        },
    }

    log_path = _STATE_DIR / "watcher.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")
    except Exception as exc:
        hook_log("watcher_log_open_failed", {"error": str(exc)[:200]})
        log_fh = subprocess.DEVNULL

    try:
        env = os.environ.copy()
        if session_key:
            env["COGNEE_SESSION_KEY"] = session_key
        subprocess.Popen(
            [sys.executable, str(_WATCHER_SCRIPT), json.dumps(bootstrap)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        print("cognee-plugin: idle watcher started", file=sys.stderr)
    except Exception as e:
        print(f"cognee-plugin: idle watcher launch failed ({e})", file=sys.stderr)


def _find_codex_parent_pid() -> int:
    """Find the nearest live Codex ancestor, skipping hook shells."""
    fallback = os.getppid()
    try:
        raw = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        hook_log("find_codex_parent_failed", {"error": str(exc)[:200]})
        return fallback

    table: dict[int, tuple[int, str]] = {}
    for line in raw.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        table[pid] = (ppid, parts[2])

    pid = fallback
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        ppid, command = table.get(pid, (0, ""))
        executable = Path(command.split()[0]).name if command else ""
        if executable == "codex" or executable.startswith("codex-"):
            return pid
        pid = ppid
    return fallback


def _spawn_exit_watcher(
    session_id: str,
    dataset: str,
    *,
    session_key: str = "",
    agent_session_name: str = "",
    api_key: str = "",
    service_url: str = "",
) -> None:
    """Launch a detached watcher that syncs only after Codex exits."""

    def _pid_alive(pid: int) -> bool:
        if pid <= 1:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False

    # Cleanup stale watcher pidfiles so the directory does not grow forever.
    try:
        if _EXIT_WATCHERS_DIR.exists():
            for pidfile in _EXIT_WATCHERS_DIR.glob("*.pid"):
                try:
                    pid = int(pidfile.read_text(encoding="utf-8").strip())
                    if not _pid_alive(pid):
                        pidfile.unlink()
                except Exception:
                    continue
    except Exception as exc:
        hook_log("exit_watcher_prune_failed", {"error": str(exc)[:200]})

    parent_pid = _find_codex_parent_pid()
    watcher_pidfile = _EXIT_WATCHERS_DIR / f"{parent_pid}.pid"
    try:
        if watcher_pidfile.exists():
            existing = int(watcher_pidfile.read_text(encoding="utf-8").strip())
            if _pid_alive(existing):
                hook_log(
                    "exit_watcher_already_running",
                    {"parent_pid": parent_pid, "pidfile": str(watcher_pidfile)},
                )
                return
    except Exception:
        pass

    bootstrap = {
        "parent_pid": parent_pid,
        "session_id": session_id,
        "dataset": dataset,
        "session_key": session_key,
        "agent_session_name": agent_session_name,
        "api_key": api_key,
        "service_url": service_url,
        "pidfile": str(watcher_pidfile),
    }
    log_path = _STATE_DIR / "exit-watcher.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _EXIT_WATCHERS_DIR.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")
    except Exception as exc:
        hook_log("exit_watcher_log_open_failed", {"error": str(exc)[:200]})
        log_fh = subprocess.DEVNULL

    try:
        env = os.environ.copy()
        if session_key:
            env["COGNEE_SESSION_KEY"] = session_key
        subprocess.Popen(
            [sys.executable, str(_EXIT_WATCHER_SCRIPT), json.dumps(bootstrap)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        hook_log(
            "exit_watcher_started",
            {
                "parent_pid": parent_pid,
                "session_id": session_id,
                "dataset": dataset,
                "pidfile": str(watcher_pidfile),
            },
        )
    except Exception as e:
        hook_log("exit_watcher_launch_failed", {"error": str(e)[:300]})


def _purge_legacy_resolved_files() -> None:
    legacy = _STATE_DIR / "resolved.json"
    scoped_dir = _STATE_DIR / "resolved"
    try:
        if legacy.exists():
            legacy.unlink()
    except Exception as exc:
        hook_log("legacy_resolved_unlink_failed", {"error": str(exc)[:200]})
    try:
        if scoped_dir.exists():
            shutil.rmtree(scoped_dir)
    except Exception as exc:
        hook_log("legacy_resolved_dir_remove_failed", {"error": str(exc)[:200]})


@contextmanager
def _bootstrap_singleflight():
    """Allow exactly one detached bootstrap worker at a time (machine-wide).

    The marker is global because Claude and Codex share one local server, so
    only one of them should drive the boot + migration at any moment.
    """
    acquired = False
    try:
        _BOOTSTRAP_LOCK.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        if _BOOTSTRAP_LOCK.exists():
            stale = False
            try:
                raw = json.loads(_BOOTSTRAP_LOCK.read_text(encoding="utf-8"))
                pid = int(raw.get("pid", 0) or 0)
                created_at = float(raw.get("created_at", 0) or 0)
                stale = (not _pid_alive(pid)) or (now - created_at > _BOOTSTRAP_LOCK_STALE_SECONDS)
            except Exception:
                stale = True
            if stale:
                try:
                    _BOOTSTRAP_LOCK.unlink()
                except Exception as exc:
                    hook_log("bootstrap_lock_unlink_failed", {"error": str(exc)[:200]})
        try:
            fd = os.open(str(_BOOTSTRAP_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "created_at": now}, fh)
            acquired = True
        except FileExistsError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                _BOOTSTRAP_LOCK.unlink()
            except Exception as exc:
                hook_log("bootstrap_lock_release_failed", {"error": str(exc)[:200]})


def _spawn_bootstrap(
    config: dict,
    cwd: str,
    session_id: str,
    agent_session_name: str,
    session_key: str,
    dataset: str,
) -> None:
    """Launch the detached server-bootstrap worker (this script, --bootstrap)."""
    bootstrap = {
        "cwd": cwd,
        "session_id": session_id,
        "session_key": session_key,
        "dataset": dataset,
        "agent_session_name": agent_session_name,
        "service_url": str(config.get("service_url", "") or _LOCAL_SERVICE_URL),
    }
    log_path = _STATE_DIR / "bootstrap.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")
    except Exception as exc:
        hook_log("bootstrap_log_open_failed", {"error": str(exc)[:200]})
        log_fh = subprocess.DEVNULL
    try:
        env = os.environ.copy()
        if session_key:
            env["COGNEE_SESSION_KEY"] = session_key
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), _BOOTSTRAP_ARG, json.dumps(bootstrap)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        hook_log("bootstrap_spawned", {"session_id": session_id})
    except Exception as exc:
        hook_log("bootstrap_spawn_failed", {"error": str(exc)[:300]})


async def _run_heavy(
    config: dict,
    cwd: str,
    session_id: str,
    agent_session_name: str,
    session_key: str,
    dataset: str,
    *,
    managed_endpoint: bool,
    boot_timeout: float,
) -> tuple[str, str, bool]:
    """Slow session bootstrap shared by the inline (legacy) path and the
    detached worker: server boot wait, cognee init, agent registration, dataset
    creation, and the server-ready marker.

    Returns ``(user_id, agent_api_key, ok)``. ``ok`` is False only on a hard
    cloud-registration failure (mirrors the legacy early-abort).
    """
    if not managed_endpoint:
        try:
            _ensure_local_server_running(config, health_timeout=boot_timeout)
        except Exception as exc:
            hook_log("server_bootstrap_warning", {"error": str(exc)[:200]})

    # Configure cognee (cloud or local)
    try:
        await ensure_cognee_ready(config)
    except Exception as e:
        print(f"cognee-plugin: init warning ({e})", file=sys.stderr)

    # Register agent identity.
    user_id = ""
    agent_api_key = ""
    agent_id = ""
    agent_name = _resolve_agent_name(config, cwd)
    os.environ["COGNEE_AGENT_NAME"] = agent_name

    # Preferred HTTP path: create/get named agent, use its API key,
    # and register this session in agent-mode.
    if is_cloud_mode(config):
        try:
            (
                agent_id,
                agent_api_key,
                agent_name,
                _registered,
            ) = await _ensure_agent_credentials_and_register(
                config, cwd, session_id, agent_session_name, session_key
            )
            if agent_id:
                user_id = agent_id
        except Exception as exc:
            message = str(exc)[:300]
            hook_log("agent_lifecycle_error", {"error": message})
            print(f"cognee-plugin: agent lifecycle failed ({message})", file=sys.stderr)
            return "", "", False
    else:
        # Local SDK fallback path.
        try:
            if not user_id:
                user_id, fallback_key = await ensure_identity(config)
                if fallback_key and not agent_api_key:
                    agent_api_key = fallback_key
        except Exception as e:
            print(f"cognee-plugin: identity warning ({e})", file=sys.stderr)

    try:
        if user_id and is_cloud_mode(config):
            await ensure_dataset_ready_via_api(
                config.get("service_url", ""),
                agent_api_key or config.get("api_key", ""),
                dataset,
            )
        elif user_id:
            from uuid import UUID

            from cognee.modules.users.methods import get_user

            user = await get_user(UUID(user_id))
            await ensure_dataset_ready(dataset, user)
    except Exception as e:
        print(f"cognee-plugin: dataset warning ({e})", file=sys.stderr)
    if user_id:
        os.environ["COGNEE_USER_ID"] = user_id

    # Mark the server ready so hot-path recall can engage — only once it is
    # actually serving (or managed) so we never advertise a half-migrated DB.
    service_url = _normalize_service_url(str(config.get("service_url", "") or ""))
    if not service_url and not managed_endpoint:
        service_url = _LOCAL_SERVICE_URL
    if service_url and (managed_endpoint or server_health_ok(service_url, timeout=1.5)):
        mark_server_ready(service_url)

    return user_id, agent_api_key, True


async def _run_bootstrap(bootstrap: dict) -> None:
    """Detached worker body.

    Two distinct concerns, deliberately decoupled:
      1. Boot the local server EXACTLY ONCE — single-flighted on _BOOTSTRAP_LOCK
         so concurrent agents don't each spawn uvicorn. Workers that don't win
         the lock just wait for /health instead of returning.
      2. Register THIS agent/session — runs for EVERY agent, regardless of who
         booted, because registration is per-agent (and concurrency-safe via
         the agent-keys / agent-lifecycle locks inside _run_heavy).
    """
    config = load_config()
    cwd = str(bootstrap.get("cwd") or os.getcwd())
    session_id = str(bootstrap.get("session_id", "") or "")
    session_key = str(bootstrap.get("session_key", "") or "")
    dataset = str(bootstrap.get("dataset", "") or get_dataset(config))
    agent_session_name = str(bootstrap.get("agent_session_name", "") or session_key)
    if session_key:
        os.environ["COGNEE_SESSION_KEY"] = session_key
    os.environ["COGNEE_AGENT_MODE"] = "true"
    config["service_url"] = _LOCAL_SERVICE_URL
    os.environ["COGNEE_SERVICE_URL"] = _LOCAL_SERVICE_URL

    # 1. Ensure the server is up. Only the single-flight winner spawns uvicorn;
    #    everyone else waits for /health (the winner may still be migrating).
    if not _health_ok():
        with _bootstrap_singleflight() as acquired:
            if acquired:
                try:
                    _ensure_local_server_running(
                        config, health_timeout=_SERVER_BOOT_DEADLINE_SECONDS
                    )
                except Exception as exc:
                    hook_log("server_bootstrap_warning", {"error": str(exc)[:200]})
            else:
                hook_log("bootstrap_waiting_for_peer", {"session_id": session_id})
        if not _wait_for_health(_SERVER_BOOT_DEADLINE_SECONDS):
            hook_log("bootstrap_server_unhealthy", {"session_id": session_id})
            return

    # 2. Register this agent/session. Runs for every agent — NOT gated by the
    #    boot single-flight. _run_heavy's _ensure_local_server_running is a
    #    no-op now that the server is healthy.
    try:
        await _run_heavy(
            config,
            cwd,
            session_id,
            agent_session_name,
            session_key,
            dataset,
            managed_endpoint=False,
            boot_timeout=_SERVER_BOOT_DEADLINE_SECONDS,
        )
        hook_log("bootstrap_complete", {"session_id": session_id})
    except Exception as exc:
        hook_log("bootstrap_failed", {"error": str(exc)[:300]})


async def _start(payload: dict | None = None) -> dict:
    config = load_config()
    payload = payload or {}
    cwd = str(payload.get("cwd") or os.environ.get("CODEX_CWD") or os.getcwd())
    explicit_service_url = str(config.get("service_url", "") or "").strip()
    explicit_api_key = str(config.get("api_key", "") or "").strip()
    managed_endpoint = bool(explicit_service_url and explicit_api_key)

    if managed_endpoint:
        os.environ["COGNEE_AGENT_MODE"] = "false"
        os.environ["COGNEE_SERVICE_URL"] = explicit_service_url
        os.environ["COGNEE_API_KEY"] = explicit_api_key
        hook_log(
            "endpoint_mode_selected",
            {"mode": "managed_endpoint", "service_url": explicit_service_url},
        )
    else:
        # Local agent mode. The service URL is known up front; whether the
        # server is already serving is decided below (inline vs deferred boot).
        os.environ["COGNEE_AGENT_MODE"] = "true"
        config["service_url"] = _LOCAL_SERVICE_URL
        os.environ["COGNEE_SERVICE_URL"] = _LOCAL_SERVICE_URL
        hook_log(
            "endpoint_mode_selected",
            {"mode": "integration_local", "service_url": _LOCAL_SERVICE_URL},
        )

    session_id = get_session_id(config, cwd)
    payload_session_id = (
        str(payload.get("session_id", "") or "").strip() if isinstance(payload, dict) else ""
    )
    session_candidate, session_source = resolve_session_key_from_payload(payload)
    session_key = set_session_key(session_candidate)
    hook_log(
        "session_key_resolved",
        {
            "source": session_source,
            "session_key": session_key,
            "payload_session_id_present": bool(payload_session_id),
        },
    )
    if not session_key:
        hook_log("missing_payload_session_id", {"session_id": session_id, "cwd": cwd})
        print(
            "cognee-plugin: missing payload session_id; refusing to register",
            file=sys.stderr,
        )
        return {}
    agent_session_name = session_key
    os.environ["COGNEE_SESSION_KEY"] = session_key
    dataset = get_dataset(config)

    # Heavy work = agent registration + dataset creation (+ a server boot wait
    # only when the server isn't up yet). Routing:
    #   * managed endpoint or lazy disabled -> inline (legacy behavior)
    #   * lazy + server already live         -> inline; registration is fast
    #     against a healthy server, so recall works on the very first prompt
    #   * lazy + server not yet live         -> defer to the detached bootstrapper
    #     so SessionStart never blocks on a cold boot / migrations
    user_id = ""
    agent_api_key = ""
    server_live = (not managed_endpoint) and _health_ok()
    if managed_endpoint or not _LAZY_BOOTSTRAP or server_live:
        user_id, agent_api_key, ok = await _run_heavy(
            config,
            cwd,
            session_id,
            agent_session_name,
            session_key,
            dataset,
            managed_endpoint=managed_endpoint,
            boot_timeout=_HEALTH_TIMEOUT_SECONDS,
        )
        if not ok:
            if _LAZY_BOOTSTRAP and not managed_endpoint:
                # Registration against a live server failed; retry out of band
                # rather than aborting the session.
                _spawn_bootstrap(config, cwd, session_id, agent_session_name, session_key, dataset)
            else:
                return {}
    else:
        _spawn_bootstrap(config, cwd, session_id, agent_session_name, session_key, dataset)
        user_id = os.environ.get("COGNEE_USER_ID", "")

    # Remove legacy resolved cache files. Runtime state now comes from HTTP endpoints.
    _purge_legacy_resolved_files()

    # Create config file on first run if it doesn't exist
    config_file = Path.home() / ".cognee-plugin" / "config.json"
    if not config_file.exists():
        save_config(config)

    # Reset the idle clock for this Codex process before the watcher
    # starts, otherwise a stale timestamp from a prior session can cause
    # an immediate improve on startup.
    touch_activity()

    # Launch the idle watcher. If COGNEE_IDLE_DISABLED is set, skip it.
    if not config.get("service_url") and os.environ.get("COGNEE_IDLE_DISABLED", "").lower() not in (
        "1",
        "true",
        "yes",
    ):
        _spawn_idle_watcher(session_id, dataset, user_id, config, session_key)

    _spawn_exit_watcher(
        session_id,
        dataset,
        session_key=session_key,
        agent_session_name=agent_session_name,
        api_key=agent_api_key,
        service_url=str(config.get("service_url", "") or ""),
    )

    mode = "cloud" if config.get("service_url") else "local"
    print(
        f"cognee-plugin: session ready (mode={mode}, "
        f"session={session_id}, dataset={dataset}, user={user_id[:8]}...)",
        file=sys.stderr,
    )

    return {}


def main():
    # Detached bootstrap mode: run the slow server boot + registration out of
    # band so the SessionStart hook itself returns fast.
    if _BOOTSTRAP_ARG in sys.argv:
        idx = sys.argv.index(_BOOTSTRAP_ARG)
        raw = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else ""
        try:
            bootstrap = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            bootstrap = {}
        try:
            asyncio.run(_run_bootstrap(bootstrap))
        except Exception as exc:
            hook_log("bootstrap_main_exception", {"error": str(exc)[:200]})
        return

    payload_raw = sys.stdin.read()
    try:
        payload = json.loads(payload_raw) if payload_raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    # Keep SessionStart output as a valid empty hook result. Recall context is
    # injected on UserPromptSubmit, matching the original Codex hook contract.
    try:
        with quiet_hook_output("session-start"):
            asyncio.run(_start(payload))
    except Exception as exc:
        hook_log("session_start_exception", {"error": str(exc)[:200]})
    print("{}")


if __name__ == "__main__":
    main()
