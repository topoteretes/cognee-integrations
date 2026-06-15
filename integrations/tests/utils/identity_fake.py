"""Stateful in-memory fake of Cognee's auth / agent / dataset endpoints.

Reproduces the full SessionStart identity flow so the real client branch logic is
exercised end to end:

  login -> owner API key (reuse or create) -> agent create (incl. 409 -> list ->
  delete -> retry) -> users/me key validation -> register -> POST /datasets.

The class is transport-agnostic: each method takes already-parsed inputs and
returns ``(status_code, body)``. ``mock_cognee`` adapts requests to it.

Field names mirror the real backend exactly (the client breaks otherwise):
  - /auth/login            -> {"access_token": <jwt>}
  - /auth/register         -> {"id": <uuid>} (201) or 409
  - /auth/api-keys  (GET)  -> [{"key": <k>}]
  - /auth/api-keys  (POST) -> {"key": <k>}
  - /agents/create         -> {"agentId": ..., "agentApiKey": ...}  (camelCase!)
  - /agents/list           -> [{"agentEmail": "<n>@cognee.agent", "agentId": ...}]
  - /users/me              -> {"id": ...} (200) or 401
"""

from __future__ import annotations

import base64
import itertools
import json
from typing import Any

_AGENT_EMAIL_SUFFIX = "@cognee.agent"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_jwt(sub: str) -> str:
    """A structurally valid (unsigned) JWT whose payload carries ``sub``.

    The client reads ``sub`` via base64-decoding the middle segment without
    verifying the signature, so three dot-separated segments suffice.
    """
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"sub": sub}).encode())
    return f"{header}.{payload}.sig"


class IdentityFake:
    """Holds identity state for one test and answers identity endpoints.

    Defaults to the happy path (any login accepted, clean agent creation). Use the
    ``seed_*`` / ``force_*`` helpers to drive a specific branch.
    """

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self.users: dict[str, dict[str, Any]] = {}  # email -> {password, id}
        self.jwt_to_email: dict[str, str] = {}
        self.user_api_keys: dict[str, list[dict[str, str]]] = {}  # email -> [{"key": k}]
        # api_key -> {"owner": email | None, "agent": name | None, "valid": bool}
        self.valid_keys: dict[str, dict[str, Any]] = {}
        self.agents: dict[str, dict[str, str]] = {}  # name -> {agentId, agentApiKey, agentEmail}
        self.agent_id_to_name: dict[str, str] = {}
        self.datasets: dict[str, dict[str, str]] = {}  # name -> {id, name}

        # knobs
        self.reject_login = False
        self.force_register_conflict = False

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
        self.valid_keys[key] = {"owner": email, "agent": None, "valid": True}
        return key

    def seed_agent(self, name: str, api_key: str | None = None) -> dict[str, str]:
        """Pre-create an agent so the next /agents/create returns 409."""
        api_key = api_key or self._new_id("agentkey")
        record = {
            "agentId": self._new_id("agent"),
            "agentApiKey": api_key,
            "agentEmail": f"{name}{_AGENT_EMAIL_SUFFIX}",
        }
        self.agents[name] = record
        self.agent_id_to_name[record["agentId"]] = name
        self.valid_keys[api_key] = {"owner": None, "agent": name, "valid": True}
        return record

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

    def register(self, email: str) -> tuple[int, dict[str, Any]]:
        if self.force_register_conflict or email in self.users:
            return 409, {"detail": "user already exists"}
        self.seed_user(email)
        return 201, {"id": self.users[email]["id"]}

    def list_api_keys(self, auth_token: str | None) -> tuple[int, list[dict[str, str]]]:
        email = self.jwt_to_email.get(auth_token or "")
        return 200, list(self.user_api_keys.get(email or "", []))

    def create_api_key(self, auth_token: str | None) -> tuple[int, dict[str, Any]]:
        email = self.jwt_to_email.get(auth_token or "")
        if not email:
            return 401, {"detail": "not authenticated"}
        key = self._new_id("apikey")
        self.user_api_keys.setdefault(email, []).append({"key": key})
        self.valid_keys[key] = {"owner": email, "agent": None, "valid": True}
        return 200, {"key": key}

    def users_me(self, api_key: str | None) -> tuple[int, dict[str, Any]]:
        entry = self.valid_keys.get(api_key or "")
        if entry and entry["valid"]:
            owner = entry.get("owner")
            user_id = self.users.get(owner, {}).get("id") if owner else entry.get("agent")
            return 200, {"id": user_id or "user"}
        return 401, {"detail": "invalid api key"}

    def agents_create(self, name: str) -> tuple[int, dict[str, Any]]:
        if name in self.agents:
            return 409, {"detail": "agent already exists"}
        record = self.seed_agent(name)
        return 200, {"agentId": record["agentId"], "agentApiKey": record["agentApiKey"]}

    def agents_list(self) -> tuple[int, list[dict[str, str]]]:
        return 200, [
            {"agentEmail": rec["agentEmail"], "agentId": rec["agentId"]}
            for rec in self.agents.values()
        ]

    def agents_delete(self, agent_id: str) -> tuple[int, dict[str, Any]]:
        name = self.agent_id_to_name.pop(agent_id, None)
        if name:
            rec = self.agents.pop(name, None)
            if rec:
                self.valid_keys.pop(rec["agentApiKey"], None)
        return 200, {}

    def agents_register(self) -> tuple[int, dict[str, Any]]:
        return 200, {"registered": True, "activeAgents": 1}

    def agents_unregister(self) -> tuple[int, dict[str, Any]]:
        return 200, {"activeAgents": 0}

    def agents_connections_me(self) -> tuple[int, dict[str, Any]]:
        return 200, {"activeAgents": 0, "agents": []}

    def datasets_create(self, name: str) -> tuple[int, dict[str, Any]]:
        new = name not in self.datasets
        if new:
            self.datasets[name] = {"id": self._new_id("ds"), "name": name}
        return (201 if new else 200), self.datasets[name]
