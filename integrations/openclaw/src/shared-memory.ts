/**
 * Shared agent memory for the plugin's provisioned agent (SDK-646).
 *
 * A provisioned plugin agent is its own user, and cognee's grants only flow
 * child -> parent: the parent sees what the agent creates, the agent sees
 * nothing the parent (or a sibling agent) owns. Left alone, a per-plugin
 * identity would silo memory — OpenClaw could not recall what Claude Code
 * stored. Shared agent memory (the default; `sharedAgentMemory: false` or
 * `COGNEE_SHARED_AGENT_MEMORY=false` opts out into separated memory) closes
 * that with the server's existing permission model, the same way the Claude
 * Code, Codex and Hermes plugins do:
 *
 * 1. the parent owns a tenant (created if it has none — roles only exist
 *    inside a tenant) and a role named `AGENT_ROLE_NAME` in it;
 * 2. every plugin agent is a member of that tenant and role;
 * 3. the role holds read+write on the parent's datasets (backfilled on every
 *    gateway start, so datasets created later by any sibling converge);
 * 4. each dataset is addressed by UUID: a canonical, parent-owned dataset per
 *    name that every agent writes to.
 *
 * Every control-plane call authenticates as the PRINCIPAL (tenant/role
 * management is owner-only server-side, so an agent key can never widen its
 * own access); only `select_tenant` runs as the agent, on itself. Transport is
 * injected as two callables, so this module imports no HTTP code.
 */

import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { PLUGIN_VERSION } from "./version.js";

/** The role every plugin agent of a user joins; the other cognee plugins use the same name. */
export const AGENT_ROLE_NAME = "cognee-agent";
/** Where the wiring outcome is remembered, next to the identity cache. */
export const SHARED_MEMORY_MARKER_PATH = join(homedir(), ".cognee-plugin", "openclaw", "shared_memory.json");
/** Outcomes no retry within this deployment can change: under identity mode
 *  `auto` an install that hit one stays on the principal until the plugin is updated. */
export const STRUCTURAL_FAILURES: ReadonlySet<string> = new Set(["unsupported", "not_tenant_owner", "tenantless_with_data"]);
const GRANT_DENIED_RETRY_MS = 3_600_000;

/** `call(method, path, body?)` → `{status, body}`: status is 200 for any 2xx, the
 *  HTTP status otherwise, 0 when the server never answered. Never throws. */
export type ControlPlane = (method: string, path: string, body?: unknown) => Promise<{ status: number; body: unknown }>;

export interface SharedOutcome {
  mode: "shared" | "separated";
  reason: string;
  /** The canonical UUID to write `dataset` under ("" when separated or no dataset asked). */
  datasetId: string;
  /** The UUIDs to recall `dataset` from. */
  datasetIds: string[];
  roleId: string;
  dataset: string;
}

export interface SharedMarker {
  base_url: string;
  mode: "shared" | "separated";
  reason: string;
  tenant_id?: string;
  role_id?: string;
  parent_user_id?: string;
  agent_id?: string;
  granted?: Record<string, "ok" | { denied_at: number }>;
  canonical?: Record<string, string>;
  updated_at?: string;
  plugin_version?: string;
}

/** Shared agent memory is on unless the user opted out (env wins over config). */
export function sharedMemoryEnabled(configValue?: boolean | string | null): boolean {
  const raw = (process.env.COGNEE_SHARED_AGENT_MEMORY ?? "").trim().toLowerCase();
  const value = raw !== "" ? raw : configValue;
  if (value === undefined || value === null || value === "") return true;
  if (typeof value === "boolean") return value;
  return !["0", "false", "no", "off"].includes(String(value).trim().toLowerCase());
}

export function pluginVersion(): string {
  return PLUGIN_VERSION;
}

export function separated(reason: string, dataset = ""): SharedOutcome {
  return { mode: "separated", reason, datasetId: "", datasetIds: [], roleId: "", dataset };
}

function normalizeUrl(url: string): string {
  return (url ?? "").trim().replace(/\/+$/, "");
}

// -- marker -------------------------------------------------------------------

export async function loadMarker(baseUrl: string, path = SHARED_MEMORY_MARKER_PATH): Promise<Partial<SharedMarker>> {
  let data: Partial<SharedMarker>;
  try {
    data = JSON.parse(await readFile(path, "utf-8")) as Partial<SharedMarker>;
  } catch {
    return {};
  }
  if (!data || typeof data !== "object") return {};
  const recorded = normalizeUrl(String(data.base_url ?? ""));
  if (recorded && normalizeUrl(baseUrl) && recorded !== normalizeUrl(baseUrl)) return {};
  return data;
}

export async function saveMarker(marker: Partial<SharedMarker>, path = SHARED_MEMORY_MARKER_PATH): Promise<void> {
  const record = { ...marker, updated_at: new Date().toISOString(), plugin_version: pluginVersion() };
  await mkdir(dirname(path), { recursive: true });
  const tmp = `${path}.${process.pid}.tmp`;
  await writeFile(tmp, JSON.stringify(record, null, 2), "utf-8");
  await rename(tmp, path);
}

// -- control plane -----------------------------------------------------------

/** A 2xx, or a 409 — the server's "already exists" for idempotent adds. */
const accepted = (status: number) => status === 200 || status === 409;

function rowStr(row: unknown, ...keys: string[]): string {
  if (!row || typeof row !== "object") return "";
  for (const key of keys) {
    const value = (row as Record<string, unknown>)[key];
    if (value) return String(value).trim();
  }
  return "";
}

interface DatasetRow { id: string; name: string; owner_id: string; created_at: string }

export async function userMe(principal: ControlPlane): Promise<{ id: string; tenant_id: string } | undefined> {
  const { status, body } = await principal("GET", "/api/v1/users/me");
  if (status !== 200 || !body || typeof body !== "object") return undefined;
  const id = rowStr(body, "id");
  return id ? { id, tenant_id: rowStr(body, "tenant_id", "tenantId") } : undefined;
}

export async function listDatasets(principal: ControlPlane): Promise<DatasetRow[]> {
  const { status, body } = await principal("GET", "/api/v1/datasets");
  if (status !== 200 || !Array.isArray(body)) return [];
  const rows: DatasetRow[] = [];
  for (const item of body) {
    const id = rowStr(item, "id");
    const name = rowStr(item, "name");
    if (id && name) rows.push({ id, name, owner_id: rowStr(item, "owner_id", "ownerId"), created_at: rowStr(item, "created_at", "createdAt") });
  }
  return rows;
}

export async function createDataset(principal: ControlPlane, name: string): Promise<{ id: string; name: string } | undefined> {
  const { status, body } = await principal("POST", "/api/v1/datasets", { name });
  if (status !== 200) return undefined;
  const id = rowStr(body, "id");
  return id ? { id, name: rowStr(body, "name") || name } : undefined;
}

/** Does this server expose the permissions API? Probed on the one endpoint that
 *  cannot 404 for a missing tenant. */
export async function permissionsSupported(principal: ControlPlane): Promise<boolean> {
  const { status } = await principal("GET", "/api/v1/permissions/tenants/me");
  return status !== 404 && status !== 405;
}

async function ensureTenant(principal: ControlPlane, parent: { id: string; tenant_id: string }, datasets: DatasetRow[]): Promise<[string, string]> {
  if (parent.tenant_id) return [parent.tenant_id, ""];
  // Activating a tenant re-scopes dataset visibility to it, which would hide
  // every dataset created under no tenant: such installs stay separated.
  if (datasets.length > 0) return ["", "tenantless_with_data"];
  const name = encodeURIComponent(`cognee-${parent.id.slice(0, 8)}`);
  const { status, body } = await principal("POST", `/api/v1/permissions/tenants?tenant_name=${name}`);
  if (status !== 200 || !body || typeof body !== "object") return ["", "tenant_create_failed"];
  return [rowStr(body, "tenant_id", "tenantId"), ""];
}

async function ensureRole(principal: ControlPlane, tenantId: string): Promise<[string, string]> {
  const find = async (): Promise<string> => {
    const { status, body } = await principal("GET", `/api/v1/permissions/tenants/${tenantId}/roles`);
    if (status === 200 && Array.isArray(body)) {
      for (const row of body) if (rowStr(row, "name") === AGENT_ROLE_NAME) return rowStr(row, "id");
    }
    return "";
  };
  const existing = await find();
  if (existing) return [existing, ""];
  const { status, body } = await principal("POST", `/api/v1/permissions/roles?role_name=${encodeURIComponent(AGENT_ROLE_NAME)}`);
  if (status === 200 && rowStr(body, "role_id", "roleId")) return [rowStr(body, "role_id", "roleId"), ""];
  if (status === 409) return [await find(), ""];
  // Only the tenant owner may create roles: an org member on a shared tenant
  // cannot run shared memory — stay separated rather than fail.
  if (status === 401 || status === 403) return ["", "not_tenant_owner"];
  return ["", "role_create_failed"];
}

async function addAgentToTenantAndRole(principal: ControlPlane, agent: ControlPlane, agentId: string, tenantId: string, roleId: string): Promise<string> {
  let { status } = await principal("POST", `/api/v1/permissions/users/${agentId}/tenants?tenant_id=${tenantId}`);
  if (!accepted(status)) return status === 401 || status === 403 ? "not_tenant_owner" : "tenant_membership_failed";
  // The agent selects the tenant ITSELF: membership alone does not set its
  // active tenant, and the dataset-visibility filter compares against that.
  ({ status } = await agent("POST", "/api/v1/permissions/tenants/select", { tenant_id: tenantId }));
  if (status !== 200) return "agent_tenant_select_failed";
  ({ status } = await principal("POST", `/api/v1/permissions/users/${agentId}/roles?role_id=${roleId}`));
  if (!accepted(status)) return status === 401 || status === 403 ? "not_tenant_owner" : "role_membership_failed";
  return "";
}

/** Take the agent out of the shared role (opt-out). True when it is no longer a member. */
export async function removeAgentFromRole(principal: ControlPlane, agentId: string, roleId: string): Promise<boolean> {
  if (!agentId || !roleId) return false;
  const { status } = await principal("DELETE", `/api/v1/permissions/users/${agentId}/roles?role_id=${roleId}`);
  return status === 200 || status === 404;
}

async function grantRoleOnDatasets(principal: ControlPlane, roleId: string, datasetIds: string[], marker: Partial<SharedMarker>): Promise<Set<string>> {
  const granted = (marker.granted ??= {});
  const now = Date.now();
  for (const datasetId of datasetIds) {
    const prior = granted[datasetId];
    if (prior === "ok") continue;
    if (prior && typeof prior === "object" && now - (prior.denied_at ?? 0) < GRANT_DENIED_RETRY_MS) continue;
    let outcome: "ok" | { denied_at: number } | undefined = "ok";
    for (const permission of ["read", "write"]) {
      const { status } = await principal("POST", `/api/v1/permissions/datasets/${roleId}?permission_name=${permission}`, [datasetId]);
      if (status === 401 || status === 403) {
        // Typically a dataset shared to the user read-only by someone else.
        outcome = { denied_at: now };
        break;
      }
      if (status !== 200) {
        outcome = undefined; // transient: retry next time
        break;
      }
    }
    if (outcome === undefined) {
      delete granted[datasetId];
      continue;
    }
    granted[datasetId] = outcome;
  }
  return new Set(Object.entries(granted).filter(([, v]) => v === "ok").map(([k]) => k));
}

/** The parent's own copy, else the oldest (deterministic across plugins). */
function pickCanonical(rows: DatasetRow[], parentId: string): DatasetRow {
  for (const row of rows) if (row.owner_id && row.owner_id === parentId) return row;
  return [...rows].sort((a, b) => `${a.created_at || "9"}${a.id}`.localeCompare(`${b.created_at || "9"}${b.id}`))[0];
}

// -- the wiring ---------------------------------------------------------------

export interface SharedMemoryOptions {
  baseUrl: string;
  agentId: string;
  dataset?: string;
  enabled?: boolean;
  allowSetup?: boolean;
  markerPath?: string;
  logger?: { info?: (m: string) => void; warn?: (m: string) => void; debug?: (m: string) => void };
}

/**
 * Wire (or refresh) shared agent memory for the plugin's agent and, with a
 * `dataset`, resolve its canonical UUID. Every step degrades to separated
 * memory with a reason; nothing here throws.
 */
export async function ensureSharedMemory(principal: ControlPlane, agent: ControlPlane, opts: SharedMemoryOptions): Promise<SharedOutcome> {
  const baseUrl = normalizeUrl(opts.baseUrl);
  const dataset = opts.dataset ?? "";
  const markerPath = opts.markerPath ?? SHARED_MEMORY_MARKER_PATH;
  const log = opts.logger;
  const sep = (reason: string) => separated(reason, dataset);

  if (opts.enabled === false) {
    // Opting out is a real boundary: the agent LEAVES the shared role. The
    // tenant / role / parent ids are kept so re-enabling rejoins the same role.
    const marker = await loadMarker(baseUrl, markerPath);
    if (marker.mode === "shared") {
      const removed = await removeAgentFromRole(principal, String(marker.agent_id ?? opts.agentId), String(marker.role_id ?? ""));
      if (removed) await saveMarker({ ...marker, mode: "separated", reason: "opt_out" } as SharedMarker, markerPath).catch(() => {});
      log?.info?.(`cognee-openclaw: shared memory opted out (left the shared role: ${removed})`);
    }
    return sep("opt_out");
  }
  if (!opts.agentId) return sep("no_agent_identity");

  let marker = await loadMarker(baseUrl, markerPath);
  const wired = marker.mode === "shared" && marker.agent_id === opts.agentId && !!marker.role_id && !!marker.parent_user_id;
  if (!wired && opts.allowSetup === false) return sep(String(marker.reason ?? "not_wired"));

  let datasets: DatasetRow[] | undefined;
  if (!wired) {
    if (!(await permissionsSupported(principal))) {
      await saveMarker({ base_url: baseUrl, mode: "separated", reason: "unsupported" }, markerPath).catch(() => {});
      log?.info?.("cognee-openclaw: shared memory: this server has no permissions API; memory stays separated");
      return sep("unsupported");
    }
    const parent = await userMe(principal);
    if (!parent) return sep("principal_unresolved");
    datasets = await listDatasets(principal);
    let [tenantId, reason] = await ensureTenant(principal, parent, datasets);
    let roleId = "";
    if (!reason) [roleId, reason] = await ensureRole(principal, tenantId);
    if (!reason) reason = await addAgentToTenantAndRole(principal, agent, opts.agentId, tenantId, roleId);
    if (reason) {
      await saveMarker({ base_url: baseUrl, mode: "separated", reason }, markerPath).catch(() => {});
      log?.info?.(`cognee-openclaw: shared memory not wired (${reason}); memory stays separated`);
      return sep(reason);
    }
    marker = { base_url: baseUrl, mode: "shared", reason: "", tenant_id: tenantId, role_id: roleId, parent_user_id: parent.id, agent_id: opts.agentId, granted: {}, canonical: {} };
    log?.info?.(`cognee-openclaw: shared memory wired agent ${opts.agentId} into role ${roleId}`);
  }

  const parentId = String(marker.parent_user_id ?? "");
  const roleId = String(marker.role_id ?? "");
  datasets ??= await listDatasets(principal);

  // Backfill: the role gets read+write on everything the parent can share —
  // also how datasets a sibling agent creates reach every other agent.
  let roleHolds = await grantRoleOnDatasets(principal, roleId, datasets.map((r) => r.id), marker);

  let writeId = "";
  let readIds: string[] = [];
  if (dataset) {
    const sameName = datasets.filter((r) => r.name === dataset);
    let eligible = sameName.filter((r) => r.owner_id === parentId || roleHolds.has(r.id));
    if (eligible.length === 0) {
      const created = await createDataset(principal, dataset);
      if (!created) {
        await saveMarker(marker, markerPath).catch(() => {});
        log?.warn?.(`cognee-openclaw: shared memory: could not create the canonical "${dataset}" dataset`);
        return sep("dataset_create_failed");
      }
      const row: DatasetRow = { ...created, owner_id: parentId, created_at: "" };
      sameName.push(row);
      datasets.push(row);
      for (const id of await grantRoleOnDatasets(principal, roleId, [row.id], marker)) roleHolds.add(id);
      eligible = [row];
    }
    writeId = pickCanonical(eligible, parentId).id;
    (marker.canonical ??= {})[dataset] = writeId;
    readIds = [writeId, ...sameName.filter((r) => r.id !== writeId && (roleHolds.has(r.id) || r.owner_id === opts.agentId)).map((r) => r.id)];
  }
  await saveMarker(marker, markerPath).catch(() => {});
  return { mode: "shared", reason: "", datasetId: writeId, datasetIds: readIds, roleId, dataset };
}
