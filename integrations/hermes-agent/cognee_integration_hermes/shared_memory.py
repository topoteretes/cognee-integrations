"""Shared agent memory for the plugin's provisioned agent (SDK-646).

A provisioned plugin agent is its own user, and cognee's grants only flow
child -> parent: the parent sees what the agent creates, the agent sees nothing
the parent (or a sibling agent) owns. Left alone, a per-plugin identity would
silo memory — Hermes could not recall what Claude Code stored. Shared agent
memory (the default; ``COGNEE_SHARED_AGENT_MEMORY=false`` opts out into
separated memory) closes that with the server's existing permission model, the
same way the Claude Code and Codex plugins do:

1. the parent owns a tenant (created if it has none — roles only exist inside
   a tenant) and a role named :data:`AGENT_ROLE_NAME` in it;
2. every plugin agent is a member of that tenant and role;
3. the role holds read+write on the parent's datasets (backfilled on every
   connect, so datasets created later by any sibling converge);
4. the session's dataset is addressed by UUID: a canonical, parent-owned
   dataset per name that every agent writes to, plus any other readable
   same-named copies for recall.

Every control-plane call authenticates as the PRINCIPAL (tenant/role management
is owner-only server-side, so an agent key can never widen its own access);
only ``select_tenant`` runs as the agent, on itself. Transport is injected as
two callables, so this module imports no HTTP code.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

from .config import SHARED_PLUGIN_STATE_DIR, str_to_bool
from .plugin_identity import PLUGIN_KEY, normalize_url

logger = logging.getLogger(__name__)

#: The role every plugin agent of a user joins; the Claude Code and Codex
#: plugins use the same name, so one role serves every plugin.
AGENT_ROLE_NAME = "cognee-agent"
#: Where the wiring outcome is remembered (read through the module attribute
#: at call time so tests can re-point it).
SHARED_MEMORY_MARKER = SHARED_PLUGIN_STATE_DIR / PLUGIN_KEY / "shared_memory.json"
#: Outcomes no retry within this deployment can change. Under identity mode
#: ``auto`` an install that hit one stays on the principal instead of probing
#: and provisioning again every launch — until the plugin is updated.
STRUCTURAL_FAILURES = frozenset({"unsupported", "not_tenant_owner", "tenantless_with_data"})
_GRANT_DENIED_RETRY_SECONDS = 3600.0

#: ``call(method, path, *, json_body=None, timeout=20.0) -> (status, body)``:
#: ``status`` is 200 for any 2xx, the HTTP status otherwise, 0 when the server
#: never answered. Never raises.
ControlPlane = Callable[..., tuple[int, Any]]


def shared_memory_enabled(config_value: Any = None) -> bool:
    """Shared agent memory is on unless the user opted out.

    ``COGNEE_SHARED_AGENT_MEMORY`` (env) wins over ``shared_agent_memory`` in
    ``cognee.json``; both default to on.
    """
    raw = os.environ.get("COGNEE_SHARED_AGENT_MEMORY", "")
    if str(raw).strip() == "" and config_value is not None and str(config_value).strip() != "":
        raw = config_value
    return str_to_bool(raw, True)


def plugin_version() -> str:
    try:
        from importlib.metadata import version

        return version("cognee-integration-hermes-agent")
    except Exception:
        return ""


def separated(reason: str) -> dict[str, Any]:
    return {
        "mode": "separated",
        "reason": reason,
        "dataset_id": "",
        "dataset_ids": [],
        "role_id": "",
    }


# -- marker ------------------------------------------------------------------


def _marker_path(path: Optional[Path]) -> Path:
    return Path(path) if path is not None else SHARED_MEMORY_MARKER


def load_marker(base_url: str, path: Optional[Path] = None) -> dict[str, Any]:
    """The marker for ``base_url`` (``{}`` when none, or for another server)."""
    try:
        data = json.loads(_marker_path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    recorded = normalize_url(str(data.get("base_url") or ""))
    if recorded and normalize_url(base_url) and recorded != normalize_url(base_url):
        return {}
    return data


def save_marker(marker: dict[str, Any], path: Optional[Path] = None) -> None:
    marker["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # A structural reason is only structural for THIS plugin version: an
    # update may lift the limitation, so connect re-evaluates it after one.
    marker["plugin_version"] = plugin_version()
    target = _marker_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".shared_memory.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


# -- control plane ---------------------------------------------------------------


def _accepted(status: int) -> bool:
    """A 2xx, or a 409 — the server's "already exists" for idempotent adds."""
    return status == 200 or status == 409


def _row_str(row: Any, *keys: str) -> str:
    """First non-empty of several spellings (OutDTOs answer camelCase)."""
    if not isinstance(row, dict):
        return ""
    for key in keys:
        value = row.get(key)
        if value:
            return str(value).strip()
    return ""


def user_me(principal: ControlPlane) -> dict[str, str]:
    """``{"id", "tenant_id"}`` for the principal, or {}."""
    status, body = principal("GET", "/api/v1/users/me", timeout=10.0)
    if status != 200 or not isinstance(body, dict):
        return {}
    return {"id": _row_str(body, "id"), "tenant_id": _row_str(body, "tenant_id", "tenantId")}


def list_datasets(principal: ControlPlane) -> Optional[list[dict[str, str]]]:
    """Every dataset the principal can READ: ``[{"id", "name", "owner_id", "created_at"}]``.

    ``None`` when the listing could not be read (no answer, a non-2xx, a body
    that is not a list). Callers must not mistake that for "no datasets": the
    tenant step reads an empty list as permission to create — and activate —
    a tenant, which would hide every dataset created under none.
    """
    status, body = principal("GET", "/api/v1/datasets", timeout=15.0)
    if status != 200 or not isinstance(body, list):
        return None
    rows = []
    for item in body:
        dataset_id, name = _row_str(item, "id"), _row_str(item, "name")
        if dataset_id and name:
            rows.append(
                {
                    "id": dataset_id,
                    "name": name,
                    "owner_id": _row_str(item, "owner_id", "ownerId"),
                    "created_at": _row_str(item, "created_at", "createdAt"),
                }
            )
    return rows


def create_dataset(principal: ControlPlane, name: str) -> dict[str, str]:
    """``POST /api/v1/datasets`` as the principal: the (new or existing) row, or {}."""
    status, body = principal("POST", "/api/v1/datasets", json_body={"name": name}, timeout=30.0)
    if status in (301, 302, 307, 308):
        # Some deployments route the collection at the slashed path; urllib
        # refuses to replay a request with a body across the redirect.
        status, body = principal(
            "POST", "/api/v1/datasets/", json_body={"name": name}, timeout=30.0
        )
    if status != 200:
        return {}
    dataset_id = _row_str(body, "id")
    return {"id": dataset_id, "name": _row_str(body, "name") or name} if dataset_id else {}


def permissions_supported(principal: ControlPlane) -> bool:
    """Does this server expose the permissions API (tenants/roles/grants)?

    Probed on the one endpoint that cannot 404 for any other reason: several
    permission routes answer 404 for a *missing tenant*, so a 404 elsewhere is
    not a capability verdict.
    """
    status, _ = principal("GET", "/api/v1/permissions/tenants/me", timeout=10.0)
    return status not in (404, 405)


def _ensure_tenant(principal: ControlPlane, parent: dict, datasets: list[dict]) -> tuple[str, str]:
    """``(tenant_id, reason)``: the parent's active tenant, created when it has
    none — but only when no dataset it can read exists yet. Activating a tenant
    re-scopes dataset visibility to it, which would hide every dataset created
    under no tenant; such installs stay separated (``tenantless_with_data``)."""
    tenant_id = str(parent.get("tenant_id") or "")
    if tenant_id:
        return tenant_id, ""
    if datasets:
        return "", "tenantless_with_data"
    name = quote(f"cognee-{str(parent.get('id') or '')[:8]}")
    status, body = principal("POST", f"/api/v1/permissions/tenants?tenant_name={name}")
    if status != 200 or not isinstance(body, dict):
        logger.debug("shared memory: tenant create failed (HTTP %s)", status)
        return "", "tenant_create_failed"
    return _row_str(body, "tenant_id", "tenantId"), ""


def _ensure_role(principal: ControlPlane, tenant_id: str) -> tuple[str, str]:
    """Get-or-create :data:`AGENT_ROLE_NAME` in ``tenant_id``: ``(role_id, reason)``."""

    def _find() -> str:
        status, body = principal(
            "GET", f"/api/v1/permissions/tenants/{tenant_id}/roles", timeout=10.0
        )
        if status == 200 and isinstance(body, list):
            for row in body:
                if _row_str(row, "name") == AGENT_ROLE_NAME:
                    return _row_str(row, "id")
        return ""

    role_id = _find()
    if role_id:
        return role_id, ""
    status, body = principal(
        "POST", f"/api/v1/permissions/roles?role_name={quote(AGENT_ROLE_NAME)}"
    )
    if status == 200 and _row_str(body, "role_id", "roleId"):
        return _row_str(body, "role_id", "roleId"), ""
    if status == 409:
        return _find(), ""
    if status in (401, 403):
        # Only the tenant owner may create roles: an org member on a shared
        # tenant cannot run shared memory — stay separated rather than fail.
        return "", "not_tenant_owner"
    logger.debug("shared memory: role create failed (HTTP %s)", status)
    return "", "role_create_failed"


def _add_agent_to_tenant_and_role(
    principal: ControlPlane, agent: ControlPlane, agent_id: str, tenant_id: str, role_id: str
) -> str:
    """Membership wiring for one agent; returns a failure reason or ""."""
    status, _ = principal(
        "POST", f"/api/v1/permissions/users/{agent_id}/tenants?tenant_id={tenant_id}"
    )
    if not _accepted(status):
        return "not_tenant_owner" if status in (401, 403) else "tenant_membership_failed"
    # The agent selects the tenant ITSELF: membership alone does not set its
    # active tenant, and the dataset-visibility filter compares against that.
    status, _ = agent(
        "POST", "/api/v1/permissions/tenants/select", json_body={"tenant_id": tenant_id}
    )
    if status != 200:
        logger.debug("shared memory: agent could not select the tenant (HTTP %s)", status)
        return "agent_tenant_select_failed"
    status, _ = principal("POST", f"/api/v1/permissions/users/{agent_id}/roles?role_id={role_id}")
    if not _accepted(status):
        return "not_tenant_owner" if status in (401, 403) else "role_membership_failed"
    return ""


def remove_agent_from_role(principal: ControlPlane, agent_id: str, role_id: str) -> bool:
    """Take the agent out of the shared role (opt-out). True when it is no
    longer a member — including when it already wasn't (404)."""
    if not (agent_id and role_id):
        return False
    status, _ = principal("DELETE", f"/api/v1/permissions/users/{agent_id}/roles?role_id={role_id}")
    if status not in (200, 404):
        logger.debug("shared memory: leaving the role failed (HTTP %s)", status)
        return False
    return True


def _grant_role_on_datasets(
    principal: ControlPlane, role_id: str, dataset_ids: list[str], marker: dict
) -> set[str]:
    """Give the role read+write on the datasets it does not hold yet.

    One POST per permission for the whole batch (the route takes a list);
    only when the batch is refused (401/403 — typically one dataset shared to
    the user read-only by someone else, which the user cannot share on) does
    it fall back to one call per dataset, so a single unshareable dataset
    cannot fail the rest. Outcomes are memoised in ``marker["granted"]``:
    ``"ok"`` never retried, a denial retried hourly, anything transient (a
    5xx, no answer, the deadline) left unrecorded and retried next time.
    Returns the ids the role holds read+write on."""
    granted = marker.setdefault("granted", {})
    if not isinstance(granted, dict):
        granted = marker["granted"] = {}
    now = time.time()
    pending = []
    for dataset_id in dataset_ids:
        prior = granted.get(dataset_id)
        if prior == "ok":
            continue
        if isinstance(prior, dict) and now - float(prior.get("denied_at") or 0) < (
            _GRANT_DENIED_RETRY_SECONDS
        ):
            continue
        pending.append(dataset_id)

    def _grant(ids: list[str], permission: str) -> int:
        status, _ = principal(
            "POST",
            f"/api/v1/permissions/datasets/{role_id}?permission_name={permission}",
            json_body=ids,
        )
        return status

    if pending:
        outcomes: dict[str, Any] = {dataset_id: "ok" for dataset_id in pending}
        for permission in ("read", "write"):
            status = _grant(pending, permission)
            if status == 200:
                continue
            if status not in (401, 403):
                outcomes = {dataset_id: None for dataset_id in pending}  # transient
                break
            # The batch was refused: find out which dataset(s), one at a time.
            for dataset_id in pending:
                if outcomes.get(dataset_id) != "ok":
                    continue
                single = _grant([dataset_id], permission)
                if single in (401, 403):
                    outcomes[dataset_id] = {"denied_at": now}
                elif single != 200:
                    outcomes[dataset_id] = None
        for dataset_id, outcome in outcomes.items():
            if outcome is None:
                granted.pop(dataset_id, None)
            else:
                granted[dataset_id] = outcome
    return {dataset_id for dataset_id, outcome in granted.items() if outcome == "ok"}


def _with_deadline(call: ControlPlane, deadline: Optional[float]) -> ControlPlane:
    """Bound every call by what is left of an absolute ``time.monotonic()`` deadline;
    past it, calls answer ``(0, …)`` without touching the network."""
    if deadline is None:
        return call

    def bounded(method: str, path: str, *, json_body=None, timeout: float = 20.0):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0, {"error": "shared memory deadline exceeded"}
        return call(method, path, json_body=json_body, timeout=min(timeout, remaining))

    return bounded


def _pick_canonical(rows: list[dict], parent_id: str) -> dict:
    """The canonical dataset among same-named rows: the parent's own copy,
    else the oldest (deterministic across plugins, so siblings converge)."""
    for row in rows:
        if row["owner_id"] and row["owner_id"] == parent_id:
            return row
    return sorted(rows, key=lambda r: (r["created_at"] or "9", r["id"]))[0]


# -- the wiring --------------------------------------------------------------


def ensure_shared_memory(
    principal: ControlPlane,
    agent: ControlPlane,
    *,
    base_url: str,
    agent_id: str,
    dataset: str = "",
    enabled: bool = True,
    allow_setup: bool = True,
    marker_path: Optional[Path] = None,
    budget: Optional[float] = None,
) -> dict[str, Any]:
    """Wire (or refresh) shared agent memory for the plugin's agent.

    ``budget`` bounds the whole wiring (seconds); a step cut off by it reads as
    transient and is retried on the next connect.

    Returns ``{"mode": "shared"|"separated", "reason", "dataset_id",
    "dataset_ids", "role_id", "dataset"}``. ``dataset_id`` is the canonical
    UUID to write ``dataset`` under and ``dataset_ids`` the UUIDs to recall
    from; both empty when separated (callers keep addressing by name) or when
    no ``dataset`` was asked for. ``allow_setup=False`` only re-resolves against
    wiring already completed. Every step degrades to separated memory with a
    logged reason; nothing here raises.
    """
    base_url = normalize_url(base_url)
    deadline = time.monotonic() + budget if budget is not None else None
    principal = _with_deadline(principal, deadline)
    agent = _with_deadline(agent, deadline)

    def _sep(reason: str) -> dict[str, Any]:
        return {**separated(reason), "dataset": dataset}

    if not enabled:
        # Opting out is a real boundary: the agent LEAVES the shared role, so it
        # can no longer read or write the user's datasets (its own remain). The
        # tenant / role / parent ids are kept — re-enabling puts the agent back
        # into the same role and canonical datasets instead of creating new ones.
        marker = load_marker(base_url, marker_path)
        if marker.get("mode") == "shared":
            removed = remove_agent_from_role(
                principal, str(marker.get("agent_id") or agent_id), str(marker.get("role_id") or "")
            )
            if removed:
                save_marker({**marker, "mode": "separated", "reason": "opt_out"}, marker_path)
            logger.info("shared memory: opted out (left the shared role: %s)", removed)
        return _sep("opt_out")
    if not agent_id:
        return _sep("no_agent_identity")

    marker = load_marker(base_url, marker_path)
    wired = (
        marker.get("mode") == "shared"
        and marker.get("agent_id") == agent_id
        and bool(marker.get("role_id"))
        and bool(marker.get("parent_user_id"))
    )
    if not wired and not allow_setup:
        return _sep(str(marker.get("reason") or "not_wired"))

    datasets: Optional[list[dict]] = None
    if not wired:
        if not permissions_supported(principal):
            save_marker(
                {"base_url": base_url, "mode": "separated", "reason": "unsupported"}, marker_path
            )
            logger.info("shared memory: this server has no permissions API; memory stays separated")
            return _sep("unsupported")
        parent = user_me(principal)
        if not parent.get("id"):
            return _sep("principal_unresolved")
        datasets = list_datasets(principal)
        if datasets is None:
            # Transient (no marker): with the listing unknown, the tenant step
            # must not run — it would read "no datasets" as safe to create one.
            logger.info("shared memory: dataset listing unreadable; retried on the next connect")
            return _sep("datasets_unreadable")
        tenant_id, reason = _ensure_tenant(principal, parent, datasets)
        role_id = ""
        if not reason:
            role_id, reason = _ensure_role(principal, tenant_id)
        if not reason:
            reason = _add_agent_to_tenant_and_role(principal, agent, agent_id, tenant_id, role_id)
        if reason:
            save_marker({"base_url": base_url, "mode": "separated", "reason": reason}, marker_path)
            logger.info("shared memory: not wired (%s); memory stays separated", reason)
            return _sep(reason)
        marker = {
            "base_url": base_url,
            "mode": "shared",
            "reason": "",
            "tenant_id": tenant_id,
            "role_id": role_id,
            "parent_user_id": parent["id"],
            "agent_id": agent_id,
            "granted": {},
            "canonical": {},
        }
        logger.info("shared memory: wired agent %s into role %s", agent_id, role_id)

    parent_id = str(marker.get("parent_user_id") or "")
    role_id = str(marker.get("role_id") or "")
    if datasets is None:
        datasets = list_datasets(principal)
    if datasets is None:
        if not wired:
            save_marker(marker, marker_path)
        return _sep("datasets_unreadable")

    # Backfill: the role gets read+write on everything the parent can share.
    # Datasets a sibling agent creates are auto-shared to the parent, so this
    # is also how they reach every other agent — no per-plugin coordination.
    role_holds = _grant_role_on_datasets(
        principal, role_id, [row["id"] for row in datasets], marker
    )

    write_id, read_ids = "", []
    if dataset:
        same_name = [row for row in datasets if row["name"] == dataset]
        eligible = [
            row for row in same_name if row["owner_id"] == parent_id or row["id"] in role_holds
        ]
        if not eligible:
            created = create_dataset(principal, dataset)
            if not created.get("id"):
                save_marker(marker, marker_path)
                logger.warning("shared memory: could not create the canonical %r dataset", dataset)
                return _sep("dataset_create_failed")
            created = {**created, "owner_id": parent_id, "created_at": ""}
            same_name.append(created)
            datasets.append(created)
            role_holds |= _grant_role_on_datasets(principal, role_id, [created["id"]], marker)
            eligible = [created]
        write_id = _pick_canonical(eligible, parent_id)["id"]
        canonical = marker.setdefault("canonical", {})
        if isinstance(canonical, dict):
            canonical[dataset] = write_id
        read_ids = [write_id] + [
            row["id"]
            for row in same_name
            if row["id"] != write_id and (row["id"] in role_holds or row["owner_id"] == agent_id)
        ]
    save_marker(marker, marker_path)
    return {
        "mode": "shared",
        "reason": "",
        "dataset_id": write_id,
        "dataset_ids": read_ids,
        "role_id": role_id,
        "dataset": dataset,
    }
