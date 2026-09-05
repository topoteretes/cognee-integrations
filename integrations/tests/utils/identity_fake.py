"""Stateful in-memory fake of Cognee's auth / agent / dataset endpoints.

Reproduces the current single-principal-key identity flow the hooks run
(session-start.py `_resolve_single_principal_key` and friends):

  env COGNEE_API_KEY -> cached key -> POST /auth/login (form) ->
  GET /auth/api-keys (cookie, reuse) -> POST /auth/api-keys (mint) ->
  GET /users/me (key probe) -> POST /agents/register ->
  GET /agents/connections/me -> POST /agents/unregister at SessionEnd.

The legacy per-agent bootstrap (auth/register, agents/create with the
409 -> list -> delete -> retry dance) was removed from the runtime and is
deliberately absent here.

The class is transport-agnostic: each method takes already-parsed inputs and
returns ``(status_code, body)``. ``mock_cognee`` adapts requests to it.

Field names mirror the real backend exactly (the client breaks otherwise):
  - /auth/login             -> {"access_token": <jwt>}
  - /auth/api-keys  (GET)   -> [{"key": <k>}]           (keys[0].key is reused)
  - /auth/api-keys  (POST)  -> {"key": <k>}
  - /users/me               -> {"id": ...} (200) or 401
  - /agents/unregister      -> {"activeAgents": <n>}
  - /agents/connections/me  -> {"agent": {"agent_session_name", "user_id",
                                          "tenant_id", "status"}}
"""

from __future__ import annotations

import base64
import itertools
import json
from typing import Any


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_jwt(sub: str) -> str:
    """A structurally valid (unsigned) JWT whose payload carries ``sub``.

    Three dot-separated base64 segments, so any client that decodes the middle
    segment without verifying the signature can read ``sub``.
    """
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"sub": sub}).encode())
    return f"{header}.{payload}.sig"


class IdentityFake:
    """Holds identity state for one test and answers identity endpoints.

    Defaults to the happy path (any login accepted, key minted on demand). Use
    the ``seed_*`` / knob attributes to drive a specific branch:
      - ``seed_owner_key``   -> GET /auth/api-keys returns it, POST mint skipped
      - ``invalidate_key``   -> GET /users/me answers 401 (re-bootstrap path)
      - ``reject_login``     -> POST /auth/login answers 401
      - ``tenant_id``        -> surfaced in /agents/connections/me
    """

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self.users: dict[str, dict[str, Any]] = {}  # email -> {password, id}
        self.jwt_to_email: dict[str, str] = {}
        self.user_api_keys: dict[str, list[dict[str, str]]] = {}  # email -> [{"key": k}]
        # api_key -> {"owner": email, "valid": bool}
        self.valid_keys: dict[str, dict[str, Any]] = {}
        self.datasets: dict[str, dict[str, str]] = {}  # name -> {id, name}
        # agent_session_name -> {"status": "active", ...}; last registered wins
        self.registered_agents: dict[str, dict[str, Any]] = {}
        self.current_agent: str = ""
        # plugin_key -> {"agent_email", "agent_id", "keys": [k, ...]}
        # (POST /integrations/plugins/{key}/provision — get-or-create + rotate)
        self.plugin_agents: dict[str, dict[str, Any]] = {}

        # knobs
        self.reject_login = False
        self.tenant_id = "tenant-test"
        # False -> provision answers 404, like a server that predates plugin
        # provisioning; the client must stay on the principal key.
        self.plugin_provisioning = True

    # -- id helpers --------------------------------------------------------
    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._counter)}"

    # -- seeding API (drive branches) -------------------------------------
    def seed_user(self, email: str, password: str = "default_password") -> None:
        self.users.setdefault(email, {"password": password, "id": self._new_id("user")})
        self.user_api_keys.setdefault(email, [])

    def seed_owner_key(self, email: str, key: str | None = None) -> str:
        """Pre-create an owner API key so the GET /auth/api-keys reuse path runs."""
        self.seed_user(email)
        key = key or self._new_id("ownerkey")
        self.user_api_keys[email].append({"key": key})
        self.valid_keys[key] = {"owner": email, "valid": True}
        return key

    def seed_api_key(self, key: str = "test-api-key", email: str = "default_user@example.com"):
        """Mark an arbitrary key (e.g. the one run_hook injects) as valid."""
        self.seed_user(email)
        self.valid_keys[key] = {"owner": email, "valid": True}
        return key

    def invalidate_key(self, key: str) -> None:
        """Mark a key invalid so GET /users/me returns 401 (re-bootstrap path)."""
        if key in self.valid_keys:
            self.valid_keys[key]["valid"] = False

    # -- endpoint logic ----------------------------------------------------
    def login(self, username: str, password: str) -> tuple[int, dict[str, Any]]:
        if self.reject_login:
            return 401, {"detail": "login rejected"}
        self.seed_user(username, password)
        jwt = make_jwt(self.users[username]["id"])
        self.jwt_to_email[jwt] = username
        return 200, {"access_token": jwt}

    def list_api_keys(self, auth_token: str | None) -> tuple[int, list[dict[str, str]]]:
        email = self.jwt_to_email.get(auth_token or "")
        return 200, list(self.user_api_keys.get(email or "", []))

    def create_api_key(self, auth_token: str | None) -> tuple[int, dict[str, Any]]:
        email = self.jwt_to_email.get(auth_token or "")
        if not email:
            return 401, {"detail": "not authenticated"}
        key = self._new_id("apikey")
        self.user_api_keys.setdefault(email, []).append({"key": key})
        self.valid_keys[key] = {"owner": email, "valid": True}
        return 200, {"key": key}

    def users_me(self, api_key: str | None) -> tuple[int, dict[str, Any]]:
        entry = self.valid_keys.get(api_key or "")
        if entry and entry["valid"]:
            owner = entry.get("owner")
            user_id = self.users.get(owner, {}).get("id") if owner else ""
            return 200, {"id": user_id or "user"}
        return 401, {"detail": "invalid api key"}

    def agents_register(self, payload: dict | None = None) -> tuple[int, dict[str, Any]]:
        payload = payload or {}
        name = str(payload.get("agent_session_name") or "")
        record = {
            "agent_session_name": name,
            "session_id": str(payload.get("session_id") or ""),
            "status": "active",
        }
        if name:
            self.registered_agents[name] = record
            self.current_agent = name
        return 200, {"registered": True, "activeAgents": len(self.registered_agents), **record}

    def agents_unregister(self, payload: dict | None = None) -> tuple[int, dict[str, Any]]:
        name = str((payload or {}).get("agent_session_name") or "")
        self.registered_agents.pop(name, None)
        if self.current_agent == name:
            self.current_agent = ""
        return 200, {"activeAgents": len(self.registered_agents)}

    def plugins_provision(
        self, plugin_key: str, api_key: str | None, *, create_only: bool = False
    ) -> tuple[int, dict[str, Any]]:
        """POST /api/v1/integrations/plugins/{plugin_key}/provision.

        Mirrors the real endpoint: idempotent get-or-create of an agent
        sub-user for the calling principal, ROTATING the key on every call
        (old keys are revoked). Response is an OutDTO -> camelCase fields.
        """
        if not self.plugin_provisioning:
            return 404, {"detail": "Not Found"}
        entry = self.valid_keys.get(api_key or "")
        if not entry or not entry["valid"]:
            return 401, {"detail": "invalid api key"}

        record = self.plugin_agents.get(plugin_key)
        if record and create_only:
            return 409, {"detail": "Identity already exists"}
        created = record is None
        if record is None:
            owner_id = self.users.get(entry["owner"], {}).get("id", "user")
            agent_email = f"{plugin_key}+{owner_id}@cognee.agent"
            self.seed_user(agent_email)
            record = {
                "agent_email": agent_email,
                "agent_id": self.users[agent_email]["id"],
                "keys": [],
            }
            self.plugin_agents[plugin_key] = record

        new_key = self._new_id("agentkey")
        self.valid_keys[new_key] = {"owner": record["agent_email"], "valid": True}
        for old_key in record["keys"]:
            self.invalidate_key(old_key)
        record["keys"] = [new_key]

        return 201, {
            "pluginKey": plugin_key,
            "agentId": record["agent_id"],
            "apiKey": new_key,
            "created": created,
        }

    def agents_connections_me(
        self, agent_session_name: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        name = agent_session_name or self.current_agent
        record = self.registered_agents.get(name or "")
        if not record:
            return 200, {"agent": None}
        owner = next(iter(self.users), "")
        agent = {
            "agent_session_name": record["agent_session_name"],
            "session_id": record["session_id"],
            "user_id": self.users.get(owner, {}).get("id", "user"),
            "tenant_id": self.tenant_id,
            "status": record["status"],
        }
        return 200, {"agent": agent}

    @property
    def principal_id(self) -> str:
        """The user id the authenticated key resolves to (mirrors connections/me)."""
        owner = next(iter(self.users), "")
        return str(self.users.get(owner, {}).get("id", "user"))

    def seed_dataset(self, name: str, owner_id: str | None = None) -> dict[str, str]:
        """Pre-create a dataset; ``owner_id`` other than the principal makes it
        readable-but-not-writable for the plugin (the switch must hide it)."""
        self.datasets[name] = {
            "id": self._new_id("ds"),
            "name": name,
            "ownerId": owner_id if owner_id is not None else self.principal_id,
        }
        return self.datasets[name]

    def datasets_create(self, name: str) -> tuple[int, dict[str, Any]]:
        new = name not in self.datasets
        if new:
            self.seed_dataset(name)
        return (201 if new else 200), self.datasets[name]

    def datasets_list(self) -> tuple[int, list[dict[str, Any]]]:
        """GET /datasets: every seeded dataset, camelCase like the real OutDTO."""
        return 200, [dict(d) for d in self.datasets.values()]
