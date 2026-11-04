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
import secrets
from typing import Any

_MINTED_KEY_PREFIXES = ("apikey", "agentkey", "ownerkey")


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
      - ``no_password_user`` -> POST /auth/login answers 400 "does not have a
                              password" (cognee >= 1.6.0 default user created
                              without DEFAULT_USER_PASSWORD)
      - ``wrong_password``   -> POST /auth/login answers 400 LOGIN_BAD_CREDENTIALS
      - ``tenant_id``        -> surfaced in /agents/connections/me
    """

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        # Minted keys carry a per-server nonce. Detached plugin processes (the
        # exit watcher, the deferred SessionEnd sync) can outlive the test that
        # started them and reach the NEXT test's server on a fixed port (the
        # local-mode e2e tests use 8011). With plain counters their cached key
        # was valid again there, and a late sync created the principal's
        # dataset before that test's shared-memory wiring ran. Now it is a 401.
        self._key_nonce = secrets.token_hex(4)
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
        # Permissions model (shared agent memory). Mirrors the server's rules
        # that matter to the client: roles live in tenants, a user's roles only
        # count while it is a member of the role's tenant, dataset visibility
        # is filtered by the caller's ACTIVE tenant, and grants require the
        # granter to hold "share" on the dataset.
        #   dataset_rows: dataset_id -> {id, name, ownerId, tenant_id, createdAt}
        #   acl:          dataset_id -> principal_id -> {permission, ...}
        self.dataset_rows: dict[str, dict[str, Any]] = {}
        self.acl: dict[str, dict[str, set[str]]] = {}
        self.tenants: dict[str, dict[str, Any]] = {}  # id -> {id, name, owner_id, members}
        self.roles: dict[str, dict[str, Any]] = {}  # id -> {id, name, tenant_id, members}
        self.active_tenant: dict[str, str | None] = {}  # user_id -> tenant_id
        self.parent_of: dict[str, str] = {}  # agent user_id -> parent user_id

        # knobs
        self.reject_login = False
        # cognee >= 1.6.0: a default user created while DEFAULT_USER_PASSWORD was
        # unset has no password at all, and login answers 400 with this detail.
        self.no_password_user = False
        # A password that does not match the stored one (fastapi-users detail).
        self.wrong_password = False
        self.tenant_id = "tenant-test"
        # False -> the plugin routes answer 404, like a server that predates
        # plugin provisioning; the client must stay on the principal key.
        self.plugin_provisioning = True
        # False -> every /permissions route answers 404, like a server that
        # predates tenants/roles; the client must stay on separated memory.
        self.permissions_api = True
        # True (default) -> the server of cognee#6113: provision only ADDS a key
        # (never revokes another) and advertises x-cognee-additive-plugin-keys on
        # the provision operation; a full agent answers 409. False models the
        # released servers, which rotate every key on provision — the plugin
        # must never provision against them.
        self.additive_keys = True
        # False -> /openapi.json's POST /remember/entry lists no dataset_id in
        # its request body, like an SDK that rejects typed session writes by
        # dataset UUID; shared memory must not wire on such a server.
        self.typed_dataset_ids = True
        # True -> /agents/register authenticates the caller's key like the real
        # route (401 for an invalid key). Off by default: most tests seed no keys.
        self.register_checks_keys = False
        # True -> /agents/register resolves dataset NAMES only among datasets the
        # caller owns, like the real route since cognee 1.6.3 (403 otherwise);
        # a shared dataset must go by id.
        self.register_checks_datasets = False
        self.max_agent_keys = 10
        # key -> server-side key id (additive servers return it; revoke-one uses it)
        self.key_ids: dict[str, str] = {}
        # key -> {"created_at", "last_used_at"} as the keys listing reports them
        self.key_meta: dict[str, dict[str, Any]] = {}

    # -- id helpers --------------------------------------------------------
    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._counter)}"

    def _new_key(self, prefix: str) -> str:
        """An API key unique to this server instance (see ``_key_nonce``)."""
        return f"{prefix}-{self._key_nonce}-{next(self._counter)}"

    def is_foreign_minted_key(self, api_key: str | None) -> bool:
        """True for a key another fake server minted (an earlier test's).

        Hand-written test keys never match the ``<prefix>-<nonce>-<n>`` shape,
        so they keep their permissive treatment; only a stale minted key from a
        detached process of an earlier test is recognised and refused.
        """
        parts = str(api_key or "").split("-")
        return (
            len(parts) == 3
            and parts[0] in _MINTED_KEY_PREFIXES
            and len(parts[1]) == 8
            and parts[2].isdigit()
            and parts[1] != self._key_nonce
        )

    # -- seeding API (drive branches) -------------------------------------
    def seed_user(self, email: str, password: str = "default_password") -> None:
        self.users.setdefault(email, {"password": password, "id": self._new_id("user")})
        self.user_api_keys.setdefault(email, [])

    def seed_owner_key(self, email: str, key: str | None = None) -> str:
        """Pre-create an owner API key so the GET /auth/api-keys reuse path runs."""
        self.seed_user(email)
        key = key or self._new_key("ownerkey")
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
        if self.no_password_user:
            return 400, {
                "detail": "This user does not have a password. Use API key authentication."
            }
        if self.wrong_password:
            return 400, {"detail": "LOGIN_BAD_CREDENTIALS"}
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
        key = self._new_key("apikey")
        self.user_api_keys.setdefault(email, []).append({"key": key})
        self.valid_keys[key] = {"owner": email, "valid": True}
        return 200, {"key": key}

    def user_id_for_key(self, api_key: str | None) -> str:
        """The user id a valid key authenticates as, or ""."""
        entry = self.valid_keys.get(api_key or "")
        if not entry or not entry["valid"]:
            return ""
        owner = entry.get("owner")
        return str(self.users.get(owner, {}).get("id") or "user") if owner else "user"

    def users_me(self, api_key: str | None) -> tuple[int, dict[str, Any]]:
        user_id = self.user_id_for_key(api_key)
        if user_id:
            return 200, {"id": user_id, "tenant_id": self.active_tenant.get(user_id)}
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

    def plugins_provision(self, plugin_key: str, api_key: str | None) -> tuple[int, dict[str, Any]]:
        """POST /api/v1/integrations/plugins/{plugin_key}/provision.

        Mirrors the real endpoint: idempotent get-or-create of an agent
        sub-user for the calling principal. ``additive_keys`` (cognee#6113)
        adds one key per call and revokes none; otherwise every call ROTATES
        (old keys are revoked), like the released servers. Response is an
        OutDTO -> camelCase fields.
        """
        if not self.plugin_provisioning:
            return 404, {"detail": "Not Found"}
        entry = self.valid_keys.get(api_key or "")
        if not entry or not entry["valid"]:
            return 401, {"detail": "invalid api key"}

        record = self.plugin_agents.get(plugin_key)
        if record and self.additive_keys:
            live = [k for k in record["keys"] if self.valid_keys.get(k, {}).get("valid")]
            if len(live) + 1 > self.max_agent_keys:
                return 409, {
                    "detail": f"The {plugin_key!r} agent already holds the maximum of "
                    f"{self.max_agent_keys} API keys."
                }
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
            # create_agent copies the parent's ACTIVE tenant (column only — no
            # membership row) and records the parent for the auto-share.
            self.parent_of[record["agent_id"]] = owner_id
            self.active_tenant[record["agent_id"]] = self.active_tenant.get(owner_id)

        new_key = self._new_key("agentkey")
        self.valid_keys[new_key] = {"owner": record["agent_email"], "valid": True}
        key_id = self._new_id("key")
        self.key_ids[new_key] = key_id
        if self.additive_keys:
            record["keys"].append(new_key)
        else:
            for old_key in record["keys"]:
                self.invalidate_key(old_key)
            record["keys"] = [new_key]

        body = {
            "pluginKey": plugin_key,
            "agentId": record["agent_id"],
            "apiKey": new_key,
            "created": created,
        }
        if self.additive_keys:
            body["keyId"] = key_id
        return 201, body

    def plugin_keys_list(self, plugin_key: str, api_key: str | None) -> tuple[int, dict[str, Any]]:
        """GET /integrations/plugins/{plugin_key}/keys: the caller's agent's keys,
        metadata only, camelCase like the real OutDTO. ``key_meta`` holds the
        timestamps a test seeds per key (created_at / last_used_at)."""
        if not self.additive_keys:
            return 404, {"detail": "Not Found"}
        if not self.user_id_for_key(api_key):
            return 401, {"detail": "invalid api key"}
        record = self.plugin_agents.get(plugin_key) or {"keys": []}
        keys = []
        for key in record["keys"]:
            if not self.valid_keys.get(key, {}).get("valid"):
                continue
            meta = self.key_meta.get(key, {})
            keys.append(
                {
                    "id": self.key_ids.get(key, ""),
                    "name": plugin_key,
                    "label": key[:8] + "****",
                    "createdAt": meta.get("created_at"),
                    "lastUsedAt": meta.get("last_used_at"),
                }
            )
        return 200, {"keys": keys}

    def integrations_status(self, api_key: str | None) -> tuple[int, dict[str, Any]]:
        """GET /integrations/status: one row per KNOWN plugin (provisioned or not)."""
        if not self.plugin_provisioning:
            return 404, {"detail": "Not Found"}
        if not self.user_id_for_key(api_key):
            return 401, {"detail": "invalid api key"}
        known = ["claude-code", "codex", "antigravity", "cursor", "openclaw", "mcp", "api"]
        plugins = []
        for plugin_key in known:
            record = self.plugin_agents.get(plugin_key)
            held = (record or {"keys": []})["keys"]
            live = [k for k in held if self.valid_keys.get(k, {}).get("valid")]
            plugins.append(
                {
                    "key": plugin_key,
                    "connected": bool(live),
                    "agentId": record["agent_id"] if record else None,
                    "source": "identity" if record else None,
                }
            )
        return 200, {"integrations": [], "plugins": plugins}

    def plugin_key_revoke(
        self, plugin_key: str, api_key: str | None, key_id: str
    ) -> tuple[int, dict[str, Any]]:
        """DELETE /integrations/plugins/{plugin_key}/keys/{key_id} (additive servers)."""
        if not self.additive_keys:
            return 404, {"detail": "Not Found"}
        if not self.user_id_for_key(api_key):
            return 401, {"detail": "invalid api key"}
        record = self.plugin_agents.get(plugin_key) or {"keys": []}
        for key in record["keys"]:
            if self.key_ids.get(key) == key_id and self.valid_keys.get(key, {}).get("valid"):
                self.invalidate_key(key)
                return 200, {"revoked": True}
        return 404, {"detail": "API key not found"}

    def plugins_disconnect(
        self, plugin_key: str, api_key: str | None
    ) -> tuple[int, dict[str, Any]]:
        """DELETE /api/v1/integrations/plugins/{plugin_key}: revoke the agent's
        keys, keep the agent user (and its data / memberships)."""
        if not self.plugin_provisioning:
            return 404, {"detail": "Not Found"}
        if not self.user_id_for_key(api_key):
            return 401, {"detail": "invalid api key"}
        record = self.plugin_agents.get(plugin_key)
        if record is None:
            return 200, {"disconnected": False}
        for key in record["keys"]:
            self.invalidate_key(key)
        return 200, {"disconnected": True}

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
        readable-but-not-writable for the plugin (the switch must hide it).

        Datasets are keyed by id in ``dataset_rows`` (two owners may hold the
        same name — the situation shared memory exists to reconcile); ``datasets``
        keeps the name -> row view older tests read, pointing at the latest row
        of that name. Like the server, the dataset takes the owner's ACTIVE
        tenant and the owner gets every permission on it; an agent's dataset is
        auto-shared (all permissions) to its parent.
        """
        owner = owner_id if owner_id is not None else self.principal_id
        for row in self.dataset_rows.values():
            if row["name"] == name and row["ownerId"] == owner:
                return row
        row = {
            "id": self._new_id("ds"),
            "name": name,
            "ownerId": owner,
            "tenant_id": self.active_tenant.get(owner),
            "createdAt": f"2026-01-01T00:00:{len(self.dataset_rows):02d}",
        }
        self.dataset_rows[row["id"]] = row
        self.datasets[name] = row
        self._grant(owner, row["id"], "read", "write", "delete", "share")
        parent = self.parent_of.get(owner)
        if parent:
            self._grant(parent, row["id"], "read", "write", "delete", "share")
        elif owner != self.principal_id:
            # A foreign-owned seed models "shared with the plugin read-only".
            self._grant(self.principal_id, row["id"], "read")
        return row

    def datasets_create(self, name: str, api_key: str | None = None) -> tuple[int, dict[str, Any]]:
        """POST /datasets as the key's user (the principal when no key is given)."""
        if self.is_foreign_minted_key(api_key):
            # A detached process left over from an earlier test, holding that
            # test's minted key, must not create the principal's dataset here:
            # it changed what this test's SessionStart saw (tenantless_with_data).
            return 401, {"detail": "invalid api key"}
        owner = self.user_id_for_key(api_key) if api_key else self.principal_id
        owner = owner or self.principal_id
        new = not any(
            r["name"] == name and r["ownerId"] == owner for r in self.dataset_rows.values()
        )
        row = self.seed_dataset(name, owner)
        return (201 if new else 200), row

    def datasets_list(self, api_key: str | None = None) -> tuple[int, list[dict[str, Any]]]:
        """GET /datasets: the datasets the caller can READ (every seeded one
        when no key is given), camelCase like the real OutDTO."""
        if not api_key:
            return 200, [dict(d) for d in self.dataset_rows.values()]
        user_id = self.user_id_for_key(api_key)
        if not user_id:
            return 401, {"detail": "invalid api key"}
        return 200, [dict(self.dataset_rows[ds]) for ds in self.readable_dataset_ids(user_id)]

    # -- permissions model (tenants / roles / grants) -----------------------
    def _grant(self, principal_id: str, dataset_id: str, *permissions: str) -> None:
        self.acl.setdefault(dataset_id, {}).setdefault(principal_id, set()).update(permissions)

    def _principals_of(self, user_id: str) -> set[str]:
        """The user plus the roles it holds — but, like the server, a role only
        counts while the user is a member of the role's tenant."""
        principals = {user_id}
        for role in self.roles.values():
            tenant = self.tenants.get(role["tenant_id"])
            if user_id in role["members"] and tenant and user_id in tenant["members"]:
                principals.add(role["id"])
        return principals

    def permitted_dataset_ids(self, user_id: str, permission: str) -> list[str]:
        """Datasets ``user_id`` holds ``permission`` on, in the user's active tenant."""
        principals = self._principals_of(user_id)
        active = self.active_tenant.get(user_id)
        out = []
        for dataset_id, row in self.dataset_rows.items():
            if row.get("tenant_id") != active:
                continue
            grants = self.acl.get(dataset_id, {})
            if any(permission in grants.get(p, set()) for p in principals):
                out.append(dataset_id)
        return out

    def readable_dataset_ids(self, user_id: str) -> list[str]:
        return self.permitted_dataset_ids(user_id, "read")

    def principal_datasets(self, principal_id: str, permission: str) -> list[dict[str, Any]]:
        """GET /permissions/principals/{id}/datasets: the principal's DIRECT
        grants only — like the server, no role expansion and no tenant filter."""
        return [
            dict(row)
            for dataset_id, row in self.dataset_rows.items()
            if permission in self.acl.get(dataset_id, {}).get(principal_id, set())
        ]

    def writable_dataset_ids(self, user_id: str) -> list[str]:
        return self.permitted_dataset_ids(user_id, "write")

    def _auth(self, api_key: str | None) -> tuple[str, tuple[int, Any] | None]:
        if not self.permissions_api:
            return "", (404, {"detail": "Not Found"})
        user_id = self.user_id_for_key(api_key)
        if not user_id:
            return "", (401, {"detail": "invalid api key"})
        return user_id, None

    def tenants_me(self, api_key: str | None) -> tuple[int, Any]:
        user_id, err = self._auth(api_key)
        if err:
            return err
        return 200, [
            {"id": t["id"], "name": t["name"]}
            for t in self.tenants.values()
            if user_id in t["members"]
        ]

    def tenants_create(self, api_key: str | None, name: str) -> tuple[int, Any]:
        """POST /permissions/tenants: owner = caller, made a member and ACTIVE."""
        user_id, err = self._auth(api_key)
        if err:
            return err
        tenant = {
            "id": self._new_id("tenant"),
            "name": name,
            "owner_id": user_id,
            "members": {user_id},
        }
        self.tenants[tenant["id"]] = tenant
        self.active_tenant[user_id] = tenant["id"]
        return 200, {"message": "Tenant created.", "tenant_id": tenant["id"]}

    def tenant_add_user(
        self, api_key: str | None, target_id: str, tenant_id: str
    ) -> tuple[int, Any]:
        user_id, err = self._auth(api_key)
        if err:
            return err
        tenant = self.tenants.get(tenant_id)
        if not tenant:
            return 404, {"detail": "Could not find tenant"}
        if tenant["owner_id"] != user_id:
            return 403, {"detail": "Only tenant owner can add other users to organization."}
        if target_id in tenant["members"]:
            return 409, {"detail": "User is already part of group."}
        tenant["members"].add(target_id)
        return 200, {"message": "User added to tenant"}

    def tenant_select(self, api_key: str | None, tenant_id: str | None) -> tuple[int, Any]:
        user_id, err = self._auth(api_key)
        if err:
            return err
        if tenant_id is None:
            self.active_tenant[user_id] = None
            return 200, {"message": "Tenant selected.", "tenant_id": None}
        tenant = self.tenants.get(tenant_id)
        if not tenant or user_id not in tenant["members"]:
            return 404, {"detail": "User is not part of the tenant."}
        self.active_tenant[user_id] = tenant_id
        return 200, {"message": "Tenant selected.", "tenant_id": tenant_id}

    def tenant_roles(self, api_key: str | None, tenant_id: str) -> tuple[int, Any]:
        user_id, err = self._auth(api_key)
        if err:
            return err
        return 200, [
            {"id": r["id"], "name": r["name"], "user_count": len(r["members"])}
            for r in self.roles.values()
            if r["tenant_id"] == tenant_id
        ]

    def roles_create(self, api_key: str | None, name: str) -> tuple[int, Any]:
        """POST /permissions/roles: in the caller's ACTIVE tenant, owner-only."""
        user_id, err = self._auth(api_key)
        if err:
            return err
        tenant = self.tenants.get(self.active_tenant.get(user_id) or "")
        if not tenant:
            return 404, {"detail": "Could not find tenant"}
        if tenant["owner_id"] != user_id:
            return 403, {
                "detail": (
                    "User submitting request does not have permission to create role for tenant."
                )
            }
        if any(r["name"] == name and r["tenant_id"] == tenant["id"] for r in self.roles.values()):
            return 409, {"detail": "Role already exists for tenant."}
        role = {
            "id": self._new_id("role"),
            "name": name,
            "tenant_id": tenant["id"],
            "members": set(),
        }
        self.roles[role["id"]] = role
        return 200, {
            "message": "Role created for tenant",
            "role_id": role["id"],
            "tenant_id": tenant["id"],
        }

    def role_add_user(self, api_key: str | None, target_id: str, role_id: str) -> tuple[int, Any]:
        user_id, err = self._auth(api_key)
        if err:
            return err
        role = self.roles.get(role_id)
        if not role:
            return 404, {"detail": "Role not found"}
        tenant = self.tenants[role["tenant_id"]]
        if target_id not in tenant["members"]:
            return 404, {
                "detail": "User tenant does not match role tenant. User cannot be added to role."
            }
        if tenant["owner_id"] != user_id:
            return 403, {
                "detail": "User submitting request does not have permission to add user to role."
            }
        if target_id in role["members"]:
            return 409, {"detail": "User is already part of group."}
        role["members"].add(target_id)
        return 200, {"message": "User added to role"}

    def role_remove_user(
        self, api_key: str | None, target_id: str, role_id: str
    ) -> tuple[int, Any]:
        """DELETE /permissions/users/{id}/roles?role_id=: tenant owner only."""
        user_id, err = self._auth(api_key)
        if err:
            return err
        role = self.roles.get(role_id)
        if not role:
            return 404, {"detail": "Role not found"}
        if self.tenants[role["tenant_id"]]["owner_id"] != user_id:
            return 403, {"detail": "User is not authorized to manage users for this tenant"}
        if target_id not in role["members"]:
            return 404, {"detail": "User is not part of the role."}
        role["members"].discard(target_id)
        return 200, {"message": "User removed from role"}

    def grant_datasets(
        self, api_key: str | None, principal_id: str, dataset_ids: list[str], permission: str
    ) -> tuple[int, Any]:
        """POST /permissions/datasets/{principal}: the caller needs "share" on
        EVERY requested dataset, else the whole call is denied (server semantics)."""
        user_id, err = self._auth(api_key)
        if err:
            return err
        shareable = set(self.permitted_dataset_ids(user_id, "share"))
        if any(ds not in shareable for ds in dataset_ids):
            return 403, {"detail": "Request owner does not have necessary permission: [share]"}
        for ds in dataset_ids:
            self._grant(principal_id, ds, permission)
        return 200, {"message": "Permission assigned to principal"}
