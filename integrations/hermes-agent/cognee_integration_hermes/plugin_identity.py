"""Plugin identity: a dedicated cognee agent for this plugin (SDK-646).

A cognee user can give each plugin its own *agent*: a sub-user with its own API
keys, so the integrations page attributes traffic per plugin and one plugin's
key can be revoked without touching the others. The server hands one out through
``POST /api/v1/integrations/plugins/{plugin_key}/provision``, called as the
user (the *principal*). The plugin describes itself in that call — key, label
and connection type — so the server needs no hardcoded list of plugins.

``COGNEE_PLUGIN_IDENTITY`` (or ``plugin_identity`` in ``cognee.json``) selects
the policy, mirroring the Claude Code / Codex plugins:

* ``false``: run as the principal; a cached identity is ignored.
* ``auto`` (default): identity in service of shared agent memory. The HTTP
  backend provisions when shared memory is on (and no structural failure was
  recorded for this plugin version), wires the agent into the user's tenant
  role and canonical datasets (:mod:`shared_memory`), and reverts a fresh
  identity that cannot be wired — so the user's datasets never end up behind
  an agent that cannot reach them. :func:`ensure_identity` itself returns the
  principal for ``auto``; the backend drives the policy.
* ``true``: explicit identity. Provision when nothing is cached and run every
  call as the agent; anything that prevents that is an error, never a quiet
  fall back to the principal. Memory written as the agent is attributed to the
  ``hermes-agent`` plugin on the integrations page; the principal sees it, the
  agent does not see the principal's other datasets.

Provisioning is attempted only against a server whose provision route adds one
key per machine (the ``x-cognee-additive-plugin-keys`` OpenAPI marker,
cognee#6113): on older servers every provision rotated the agent's keys, which
would log out every other machine.

Stdlib only. The transport is injected as a ``request`` callable so this module
never imports the HTTP backend (which imports it).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .config import SHARED_PLUGIN_STATE_DIR

logger = logging.getLogger(__name__)

#: The key the server knows this plugin by: the agent's name, the provision
#: route's path parameter and the ``metadata.plugin_key`` on every connection.
PLUGIN_KEY = "hermes-agent"
#: Self-declared connection type (``type`` on ``POST /api/v1/agents/register``).
CONNECTION_TYPE = "hermes_agent"
#: Human-readable name the server stores alongside the identity.
PLUGIN_LABEL = "Hermes Agent"
#: OpenAPI marker on the provision operation of servers that only ADD keys.
ADDITIVE_KEYS_MARKER = "x-cognee-additive-plugin-keys"
PROVISION_PATH = f"/api/v1/integrations/plugins/{PLUGIN_KEY}/provision"
KEYS_COMMAND_HINT = (
    f"hermes cognee keys list --plugin {PLUGIN_KEY} / "
    f"hermes cognee keys revoke <key_id> --plugin {PLUGIN_KEY}"
)
#: Where this machine's agent key is cached: the per-plugin layout the other
#: cognee plugins use (``~/.cognee-plugin/<plugin>/``). Read through the module
#: attribute at call time so tests can re-point it.
AGENT_KEY_CACHE = SHARED_PLUGIN_STATE_DIR / PLUGIN_KEY / "agent_key.json"

#: ``request(method, path, *, timeout, json_body=None) -> parsed JSON``; raises an
#: exception carrying ``.status`` (an HTTP error) or anything else (unreachable).
Request = Callable[..., Any]


class PluginIdentityError(RuntimeError):
    """Identity was required (``COGNEE_PLUGIN_IDENTITY=true``) and is not available."""


# -- policy ------------------------------------------------------------------


def identity_mode(value: Any = None) -> str:
    """``"auto"`` / ``"enabled"`` / ``"disabled"`` from a config or env value.

    ``None`` and ``""`` read ``COGNEE_PLUGIN_IDENTITY`` from the environment and
    default to ``auto``. Anything but auto / a boolean spelling is a config error.
    """
    if value is None or str(value).strip() == "":
        value = os.environ.get("COGNEE_PLUGIN_IDENTITY", "")
    normalized = str(value).strip().lower()
    if normalized in ("", "auto"):
        return "auto"
    if normalized in ("1", "true", "yes", "on"):
        return "enabled"
    if normalized in ("0", "false", "no", "off"):
        return "disabled"
    raise ValueError("COGNEE_PLUGIN_IDENTITY must be auto, true, or false")


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest() if key else ""


def normalize_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


# -- cache -------------------------------------------------------------------


def _cache_path(path: Optional[Path]) -> Path:
    return Path(path) if path is not None else AGENT_KEY_CACHE


def load_cached(base_url: str, path: Optional[Path] = None) -> dict[str, Any]:
    """This machine's cached identity for ``base_url``; ``{}`` for any other server."""
    target = _cache_path(path)
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(record, dict):
        return {}
    if normalize_url(str(record.get("base_url") or "")) != normalize_url(base_url):
        return {}
    return record


def save_cached(record: dict[str, Any], path: Optional[Path] = None) -> None:
    """Write the identity record, owner-readable only, atomically."""
    target = _cache_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".agent_key.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def clear_cached(path: Optional[Path] = None) -> None:
    """Drop the cached identity (a reverted provision, a dashboard disconnect)."""
    try:
        _cache_path(path).unlink(missing_ok=True)
    except Exception:
        pass


def block_cached(expected_key: str = "", path: Optional[Path] = None) -> None:
    """Mark the cached key as rejected by the server, so it is never reused.

    With ``expected_key`` set, only that key is blocked — another process may
    already have replaced it, and that replacement must not be thrown away.
    """
    target = _cache_path(path)
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(record, dict):
        return
    if expected_key and str(record.get("api_key") or "") != expected_key:
        return
    record["blocked"] = True
    try:
        save_cached(record, target)
    except Exception as exc:
        logger.debug("could not block the cached plugin identity: %s", exc)


# -- server calls --------------------------------------------------------------


def additive_keys_supported(request: Request, *, timeout: float) -> bool:
    """Does the server's provision route only ADD keys (never revoke others)?

    Read from the OpenAPI document. Anything short of an explicit ``true`` — an
    older server, an unreadable spec, a failed request — is ``False``.
    """
    try:
        spec = request("GET", "/openapi.json", timeout=timeout)
    except Exception as exc:
        logger.debug("plugin identity: could not read /openapi.json: %s", exc)
        return False
    if not isinstance(spec, dict):
        return False
    operation = spec.get("paths", {}).get(PROVISION_PATH, {})
    if not isinstance(operation, dict):
        return False
    post = operation.get("post", {})
    return isinstance(post, dict) and post.get(ADDITIVE_KEYS_MARKER) is True


def provision(request: Request, *, principal_key: str, timeout: float) -> tuple[str, dict]:
    """Add a key for this plugin's agent, as the principal.

    Verdicts: ``provisioned`` (body: ``api_key``, ``agent_id``, ``key_id``,
    ``created``), ``limit`` (the agent is full of keys in use — 409),
    ``unsupported`` (no plugin routes — 404/405) and ``failed``.
    """
    if not str(principal_key or "").strip():
        return "failed", {"reason": "no_principal_key"}
    try:
        result = request(
            "POST",
            PROVISION_PATH,
            timeout=timeout,
            # Self-description: the server learns the plugin from the
            # integration, not from a registry in its own code. Servers that
            # predate the body ignore it.
            json_body={"label": PLUGIN_LABEL, "connection_type": CONNECTION_TYPE},
        )
    except Exception as exc:
        status = getattr(exc, "status", None)
        if status in (404, 405):
            return "unsupported", {}
        if status == 409:
            return "limit", {}
        logger.debug("plugin identity: provision failed: %s", exc)
        return "failed", {"reason": f"http_{status}" if status else "unreachable"}

    if not isinstance(result, dict):
        return "failed", {"reason": "not_an_object"}
    body = {
        "api_key": str(result.get("api_key") or result.get("apiKey") or "").strip(),
        "agent_id": str(result.get("agent_id") or result.get("agentId") or ""),
        "key_id": str(result.get("key_id") or result.get("keyId") or ""),
        "created": bool(result.get("created")),
    }
    if not body["api_key"]:
        return "failed", {"reason": "incomplete"}
    if not body["agent_id"]:
        return "failed", {"reason": "missing_agent_id"}
    if body["api_key"] == str(principal_key).strip():
        # The caller's own key handed back as the "agent" key: no isolation.
        return "failed", {"reason": "agent_key_equals_principal"}
    return "provisioned", body


def ensure_identity(
    request: Request,
    *,
    base_url: str,
    principal_key: str,
    mode: str,
    timeout: float,
    cache_path: Optional[Path] = None,
) -> dict[str, Any]:
    """The identity to run as under ``mode``: ``{api_key, agent_id, source}``,
    or ``{}`` to stay on the principal.

    ``request`` must authenticate as the principal. Under ``enabled`` every
    obstacle raises :class:`PluginIdentityError`; ``auto`` and ``disabled``
    never raise.
    """
    if mode == "disabled":
        return {}
    if mode == "auto":
        # The backend decides ``auto`` (cached identity, shared memory, structural
        # markers) and calls back with ``enabled``; here it means the principal.
        return {}
    if mode != "enabled":
        raise ValueError(f"unknown plugin identity mode {mode!r}")

    principal_key = str(principal_key or "").strip()
    if not principal_key:
        raise PluginIdentityError(
            "COGNEE_PLUGIN_IDENTITY=true needs a principal API key to provision the "
            f"{PLUGIN_KEY} agent with: set COGNEE_API_KEY (or let the local server mint one)."
        )

    cached = load_cached(base_url, cache_path)
    cached_key = str(cached.get("api_key") or "").strip()
    if cached_key and not cached.get("blocked"):
        if cached.get("principal_fingerprint") != fingerprint(principal_key):
            raise PluginIdentityError(
                f"The cached {PLUGIN_KEY} identity at {_cache_path(cache_path)} was provisioned "
                "by another principal; remove the file to provision again for this one."
            )
        return {
            "api_key": cached_key,
            "agent_id": str(cached.get("agent_id") or ""),
            "key_id": str(cached.get("key_id") or ""),
            "source": "cached",
        }

    if not additive_keys_supported(request, timeout=timeout):
        raise PluginIdentityError(
            f"Plugin provisioning unsupported at {base_url}: a cognee server that adds "
            f"plugin keys per machine ({ADDITIVE_KEYS_MARKER}, cognee >= 1.6.3 with "
            "cognee#6113) is required for COGNEE_PLUGIN_IDENTITY=true."
        )
    verdict, body = provision(request, principal_key=principal_key, timeout=timeout)
    if verdict == "limit":
        raise PluginIdentityError(
            f"The {PLUGIN_KEY} agent already holds the maximum number of API keys (one per "
            f"machine, all in use). Revoke an old machine's key — {KEYS_COMMAND_HINT} — "
            "and start a new session."
        )
    if verdict == "unsupported":
        raise PluginIdentityError(
            f"Plugin provisioning unsupported at {base_url}: the server has no "
            f"{PROVISION_PATH} route (or does not know the {PLUGIN_KEY!r} plugin)."
        )
    if verdict != "provisioned":
        raise PluginIdentityError(
            f"Plugin provisioning failed at {base_url} ({body.get('reason') or 'unknown'}); "
            "owner fallback is disabled under COGNEE_PLUGIN_IDENTITY=true."
        )

    save_cached(
        {
            "base_url": normalize_url(base_url),
            "api_key": body["api_key"],
            "agent_id": body["agent_id"],
            "key_id": body["key_id"],
            "plugin_key": PLUGIN_KEY,
            "principal_fingerprint": fingerprint(principal_key),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        cache_path,
    )
    logger.info(
        "plugin identity: %s the %s agent (agent_id=%s)",
        "provisioned" if body["created"] else "added this machine's key to",
        PLUGIN_KEY,
        body["agent_id"],
    )
    return {
        "api_key": body["api_key"],
        "agent_id": body["agent_id"],
        "key_id": body["key_id"],
        "source": "provisioned",
    }
