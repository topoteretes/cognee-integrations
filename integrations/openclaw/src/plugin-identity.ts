/**
 * Plugin identity: a dedicated cognee agent for this plugin (SDK-646).
 *
 * A cognee user can give each plugin its own *agent*: a sub-user with its own
 * API keys, so the integrations page attributes traffic per plugin and one
 * plugin's key can be revoked without touching the others. The server hands one
 * out through `POST /api/v1/integrations/plugins/{plugin_key}/provision`, called
 * as the user (the *principal*). The plugin describes itself in that call — key,
 * label and connection type — so the server needs no hardcoded list of plugins.
 *
 * `pluginIdentity` (config) / `COGNEE_PLUGIN_IDENTITY` (env) selects the policy,
 * mirroring the Claude Code / Codex plugins:
 *
 * - `false`: run as the principal; a cached identity is ignored.
 * - `auto` (default): identity in service of shared agent memory.
 *   `resolveSessionIdentity` provisions when shared memory is on (and no
 *   structural failure was recorded for this plugin version), `wireSharedMemory`
 *   wires the agent into the user's tenant role and canonical datasets
 *   (shared-memory.ts), and a fresh identity that cannot be wired is reverted —
 *   so the user's datasets never end up behind an agent that cannot reach them.
 *   `ensurePluginIdentity` itself returns the principal for `auto`.
 * - `true`: explicit identity. Provision when nothing is cached and run every
 *   call as the agent. An obstacle is reported as an error, never quietly
 *   worked around: OpenClaw has no session to abort, so the gateway logs the
 *   error and continues as the principal until the cause is fixed.
 *
 * Provisioning is attempted only against a server whose provision route adds
 * one key per machine (the `x-cognee-additive-plugin-keys` OpenAPI marker,
 * cognee#6113): on older servers every provision rotated the agent's keys,
 * which would log out every other machine.
 */

import { createHash } from "node:crypto";
import { chmod, mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { requestErrorStatus } from "./client.js";
import {
  type ControlPlane,
  type SharedOutcome,
  STRUCTURAL_FAILURES,
  ensureSharedMemory,
  loadMarker,
  pluginVersion,
  separated,
} from "./shared-memory.js";

/** The key the server knows this plugin by: the agent's name, the provision
 *  route's path parameter and `metadata.plugin_key` on every connection. */
export const PLUGIN_KEY = "openclaw";
/** Self-declared connection type (`type` on `POST /api/v1/agents/register`). */
export const CONNECTION_TYPE = "openclaw";
/** Human-readable name the server stores alongside the identity. */
export const PLUGIN_LABEL = "OpenClaw";
/** OpenAPI marker on the provision operation of servers that only ADD keys. */
export const ADDITIVE_KEYS_MARKER = "x-cognee-additive-plugin-keys";
export const KEYS_COMMAND_HINT = `openclaw cognee keys --plugin ${PLUGIN_KEY} [--revoke <id>]`;
/** This machine's agent key, in the per-plugin layout the other cognee plugins
 *  use (`~/.cognee-plugin/<plugin>/`). */
export const AGENT_KEY_CACHE_PATH = join(homedir(), ".cognee-plugin", PLUGIN_KEY, "agent_key.json");

export type PluginIdentityMode = "auto" | "enabled" | "disabled";

export class PluginIdentityError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PluginIdentityError";
  }
}

/** `auto` / `enabled` / `disabled` from a config or env value; `""`/undefined is auto. */
export function parsePluginIdentityMode(value: unknown): PluginIdentityMode {
  if (value === undefined || value === null) return "auto";
  if (typeof value === "boolean") return value ? "enabled" : "disabled";
  const normalized = String(value).trim().toLowerCase();
  if (normalized === "" || normalized === "auto") return "auto";
  if (["1", "true", "yes", "on"].includes(normalized)) return "enabled";
  if (["0", "false", "no", "off"].includes(normalized)) return "disabled";
  throw new Error("pluginIdentity / COGNEE_PLUGIN_IDENTITY must be auto, true, or false");
}

export function fingerprint(key: string): string {
  return key ? createHash("sha256").update(key, "utf-8").digest("hex") : "";
}

function normalizeUrl(url: string): string {
  return (url ?? "").trim().replace(/\/+$/, "");
}

export interface AgentKeyRecord {
  base_url: string;
  api_key: string;
  agent_id: string;
  key_id: string;
  plugin_key: string;
  principal_fingerprint: string;
  updated_at: string;
  /** Set once the server rejected the key (401): never reused, replaced on the next provision. */
  blocked?: boolean;
}

/** This machine's cached identity for `baseUrl`; undefined for any other server. */
export async function loadCachedIdentity(baseUrl: string, path = AGENT_KEY_CACHE_PATH): Promise<AgentKeyRecord | undefined> {
  let record: Partial<AgentKeyRecord>;
  try {
    record = JSON.parse(await readFile(path, "utf-8")) as Partial<AgentKeyRecord>;
  } catch {
    return undefined;
  }
  if (!record || typeof record !== "object") return undefined;
  if (normalizeUrl(String(record.base_url ?? "")) !== normalizeUrl(baseUrl)) return undefined;
  return record as AgentKeyRecord;
}

/** Write the identity record, owner-readable only, atomically. */
export async function saveCachedIdentity(record: AgentKeyRecord, path = AGENT_KEY_CACHE_PATH): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const tmp = `${path}.${process.pid}.tmp`;
  await writeFile(tmp, JSON.stringify(record, null, 2), { encoding: "utf-8", mode: 0o600 });
  await chmod(tmp, 0o600).catch(() => {});
  await rename(tmp, path);
}

/**
 * Mark the cached key as rejected by the server. With `expectedKey`, only that
 * key is blocked — another process may already have replaced it.
 */
export async function blockCachedIdentity(expectedKey = "", path = AGENT_KEY_CACHE_PATH): Promise<void> {
  let record: AgentKeyRecord;
  try {
    record = JSON.parse(await readFile(path, "utf-8")) as AgentKeyRecord;
  } catch {
    return;
  }
  if (!record || typeof record !== "object") return;
  if (expectedKey && String(record.api_key ?? "") !== expectedKey) return;
  await saveCachedIdentity({ ...record, blocked: true }, path).catch(() => {});
}

export interface ProvisionResult {
  apiKey: string;
  agentId: string;
  keyId: string;
  created: boolean;
}

/** The slice of `CogneeHttpClient` the identity flow needs (fakeable in tests). */
export interface IdentityClient {
  setApiKey(key: string): void;
  additivePluginKeysSupported(pluginKey: string): Promise<boolean>;
  provisionPlugin(pluginKey: string, description: { label: string; connectionType: string }): Promise<ProvisionResult>;
}

export interface ActiveIdentity {
  apiKey: string;
  agentId: string;
  keyId: string;
  source: "cached" | "provisioned";
}

export interface IdentityLogger {
  info?: (msg: string) => void;
  warn?: (msg: string) => void;
  debug?: (msg: string) => void;
}

export interface IdentityOptions {
  baseUrl: string;
  /** The user's own key; `client` must currently authenticate with it. */
  principalKey: string;
  mode: PluginIdentityMode;
  logger?: IdentityLogger;
  cachePath?: string;
}

/**
 * The identity to run as under `mode`, or undefined to stay on the principal.
 * Under `enabled` every obstacle throws `PluginIdentityError`; `auto` and
 * `disabled` never throw. The client is left on the principal key; the caller
 * switches it to the returned key.
 */
export async function ensurePluginIdentity(client: IdentityClient, opts: IdentityOptions): Promise<ActiveIdentity | undefined> {
  const { baseUrl, mode, logger } = opts;
  const cachePath = opts.cachePath ?? AGENT_KEY_CACHE_PATH;
  if (mode === "disabled") return undefined;
  // `resolveSessionIdentity` decides `auto` (cached identity, shared memory,
  // structural markers) and calls back with `enabled`; here it means the principal.
  if (mode === "auto") return undefined;

  const principalKey = (opts.principalKey ?? "").trim();
  if (!principalKey) {
    throw new PluginIdentityError(
      `pluginIdentity: true needs a principal API key to provision the ${PLUGIN_KEY} agent with: set apiKey / COGNEE_API_KEY (or let the local server mint one).`,
    );
  }

  const cached = await loadCachedIdentity(baseUrl, cachePath);
  const cachedKey = (cached?.api_key ?? "").trim();
  if (cached && cachedKey && !cached.blocked) {
    if (cached.principal_fingerprint !== fingerprint(principalKey)) {
      throw new PluginIdentityError(
        `The cached ${PLUGIN_KEY} identity at ${cachePath} was provisioned by another principal; remove the file to provision again for this one.`,
      );
    }
    return { apiKey: cachedKey, agentId: String(cached.agent_id ?? ""), keyId: String(cached.key_id ?? ""), source: "cached" };
  }

  if (!(await client.additivePluginKeysSupported(PLUGIN_KEY))) {
    throw new PluginIdentityError(
      `Plugin provisioning unsupported at ${baseUrl}: a cognee server that adds plugin keys per machine (${ADDITIVE_KEYS_MARKER}, cognee >= 1.6.3 with cognee#6113) is required for pluginIdentity: true.`,
    );
  }

  let result: ProvisionResult;
  try {
    result = await client.provisionPlugin(PLUGIN_KEY, { label: PLUGIN_LABEL, connectionType: CONNECTION_TYPE });
  } catch (error) {
    const status = requestErrorStatus(error);
    if (status === 404 || status === 405) {
      throw new PluginIdentityError(
        `Plugin provisioning unsupported at ${baseUrl}: the server has no provision route for the ${PLUGIN_KEY} plugin.`,
      );
    }
    if (status === 409) {
      throw new PluginIdentityError(
        `The ${PLUGIN_KEY} agent already holds the maximum number of API keys (one per machine, all in use). Revoke an old machine's key — ${KEYS_COMMAND_HINT} — and restart the gateway.`,
      );
    }
    throw new PluginIdentityError(
      `Plugin provisioning failed at ${baseUrl} (${error instanceof Error ? error.message : String(error)}); owner fallback is disabled under pluginIdentity: true.`,
    );
  }

  if (!result.apiKey) throw new PluginIdentityError("Plugin provisioning returned no API key (incomplete response).");
  if (!result.agentId) throw new PluginIdentityError("Plugin provisioning returned no agent id (missing_agent_id).");
  if (result.apiKey === principalKey) {
    // The caller's own key handed back as the "agent" key: no isolation.
    throw new PluginIdentityError("Plugin provisioning handed back the principal key as the agent key (agent_key_equals_principal).");
  }

  await saveCachedIdentity(
    {
      base_url: normalizeUrl(baseUrl),
      api_key: result.apiKey,
      agent_id: result.agentId,
      key_id: result.keyId,
      plugin_key: PLUGIN_KEY,
      principal_fingerprint: fingerprint(principalKey),
      updated_at: new Date().toISOString(),
    },
    cachePath,
  );
  logger?.info?.(
    `cognee-openclaw: plugin identity: ${result.created ? "provisioned" : "added this machine's key to"} the ${PLUGIN_KEY} agent (agent_id=${result.agentId})`,
  );
  return { apiKey: result.apiKey, agentId: result.agentId, keyId: result.keyId, source: "provisioned" };
}

/** Per-gateway identity state: the principal, the agent the client runs as (if any), and its shared memory. */
export interface IdentitySession extends IdentityOptions {
  identity?: ActiveIdentity;
  /** Set when `identity` was minted by this gateway (an unwirable one is reverted under `auto`). */
  provisionedNow?: boolean;
  /** `sharedAgentMemory` (config) / `COGNEE_SHARED_AGENT_MEMORY`; default on. */
  sharedMemoryEnabled?: boolean;
  /** Shared memory wiring outcome for this gateway (see shared-memory.ts). */
  shared?: SharedOutcome;
  /** Dataset name -> canonical UUID, for every dataset resolved under shared memory. */
  canonical?: Record<string, string>;
  markerPath?: string;
}

/** The control-plane slice of `CogneeHttpClient` the wiring needs. */
export interface SharedMemoryClient extends IdentityClient {
  controlPlane(apiKey: string): ControlPlane;
  revokePluginKey(plugin: string, keyId: string): Promise<{ found: boolean }>;
}

/**
 * Pick the identity for this gateway under the configured policy and record
 * it on `session` (`identity`, `provisionedNow`).
 *
 * `enabled` provisions or throws. `auto` is identity in service of shared
 * agent memory: a cached identity bound to this principal is reused;
 * otherwise one is provisioned only when shared memory is on and no
 * structural failure was recorded for this plugin version — and never with an
 * error: the principal is the fallback. `disabled` leaves the principal.
 */
export async function resolveSessionIdentity(client: IdentityClient, session: IdentitySession): Promise<void> {
  session.identity = undefined;
  session.provisionedNow = false;
  if (session.mode === "disabled") return;
  if (session.mode === "enabled") {
    session.identity = await ensurePluginIdentity(client, session);
    session.provisionedNow = session.identity?.source === "provisioned";
    return;
  }
  const cached = await loadCachedIdentity(session.baseUrl, session.cachePath);
  if (cached && (cached.api_key ?? "").trim() && !cached.blocked) {
    if (cached.principal_fingerprint !== fingerprint(session.principalKey)) {
      session.logger?.info?.(`cognee-openclaw: the cached ${PLUGIN_KEY} identity belongs to another principal; running as the principal`);
      return;
    }
  } else if (session.sharedMemoryEnabled === false) {
    // Nothing to provision for: without shared memory the principal sees
    // everything anyway, and an agent would only silo this plugin's writes.
    return;
  } else if (!session.principalKey) {
    return;
  } else {
    const marker = await loadMarker(session.baseUrl, session.markerPath);
    if (STRUCTURAL_FAILURES.has(String(marker.reason ?? "")) && marker.plugin_version === pluginVersion()) {
      session.logger?.debug?.(`cognee-openclaw: plugin identity: not provisioning (shared memory ${marker.reason} on this server)`);
      return;
    }
  }
  try {
    session.identity = await ensurePluginIdentity(client, { ...session, mode: "enabled" });
    session.provisionedNow = session.identity?.source === "provisioned";
  } catch (error) {
    session.logger?.info?.(`cognee-openclaw: plugin identity: not provisioned (${error instanceof Error ? error.message : String(error)}); running as the principal`);
  }
}

/**
 * Wire the session's agent into shared memory (no dataset yet), or — under
 * `auto` — undo a fresh identity that cannot be wired, so nothing the
 * principal owns is ever stranded. `client` must still authenticate as the
 * principal when this runs. Records `session.shared` and `session.canonical`.
 */
export async function wireSharedMemory(client: SharedMemoryClient, session: IdentitySession): Promise<void> {
  session.canonical = {};
  if (!session.identity) {
    session.shared = separated("no_agent_identity");
    return;
  }
  session.shared = await ensureSharedMemory(client.controlPlane(session.principalKey), client.controlPlane(session.identity.apiKey), {
    baseUrl: session.baseUrl,
    agentId: session.identity.agentId,
    enabled: session.sharedMemoryEnabled !== false,
    markerPath: session.markerPath,
    logger: session.logger,
  });
  if (session.shared.mode === "shared") {
    const marker = await loadMarker(session.baseUrl, session.markerPath);
    session.canonical = { ...(marker.canonical ?? {}) };
    return;
  }
  if (session.mode === "enabled" || !session.provisionedNow) return;
  // ``auto`` provisioned on the promise of shared memory; honour it by
  // reverting: nobody will hold the key just minted, so revoke it — only it,
  // by id; other machines hold keys for the same agent.
  session.logger?.info?.(`cognee-openclaw: plugin identity: reverting to the principal (shared memory ${session.shared.reason})`);
  if (session.identity.keyId) {
    await client.revokePluginKey(PLUGIN_KEY, session.identity.keyId).catch((e) => {
      session.logger?.debug?.(`cognee-openclaw: could not revoke the unused plugin key: ${String(e)}`);
    });
  }
  await clearCachedIdentity(session.cachePath);
  session.identity = undefined;
  session.provisionedNow = false;
  session.shared = separated("no_agent_identity");
}

/**
 * The canonical UUID to address `name` by under shared memory, resolving it
 * (and creating the parent-owned dataset) on first use; undefined when the
 * session addresses datasets by name.
 */
export async function resolveSharedDataset(client: SharedMemoryClient, session: IdentitySession, name: string): Promise<string | undefined> {
  if (!name || session.shared?.mode !== "shared" || !session.identity) return undefined;
  const known = session.canonical?.[name];
  if (known) return known;
  const outcome = await ensureSharedMemory(client.controlPlane(session.principalKey), client.controlPlane(session.identity.apiKey), {
    baseUrl: session.baseUrl,
    agentId: session.identity.agentId,
    dataset: name,
    enabled: session.sharedMemoryEnabled !== false,
    markerPath: session.markerPath,
    logger: session.logger,
  });
  if (outcome.mode !== "shared" || !outcome.datasetId) {
    session.logger?.warn?.(`cognee-openclaw: shared memory: ${outcome.reason}; addressing "${name}" by name`);
    return undefined;
  }
  (session.canonical ??= {})[name] = outcome.datasetId;
  return outcome.datasetId;
}

/** The canonical UUID already resolved for `name`, if any (no network). */
export function canonicalDatasetId(session: IdentitySession, name: string): string | undefined {
  return session.shared?.mode === "shared" ? session.canonical?.[name] : undefined;
}

/** Drop the cached identity (a reverted provision, a dashboard disconnect). */
export async function clearCachedIdentity(path = AGENT_KEY_CACHE_PATH): Promise<void> {
  try {
    await unlink(path);
  } catch {
    /* already gone */
  }
}

export interface RegisterClient extends IdentityClient {
  registerAgent(params: { agentSessionName: string; sessionId?: string; datasetNames?: string[] }): Promise<{ ok: boolean; connectionId?: string }>;
}

/**
 * `client.registerAgent` under the session's identity, replacing a dead key once.
 *
 * Without an identity this is a plain registration. With one, a 401 means the
 * agent key is dead (revoked from the dashboard, or pruned): it is blocked,
 * replaced through the same provisioning, and the registration retried; a
 * second 401 — and a 403 — is an error, since owner fallback is what
 * `pluginIdentity: true` rules out. `session.identity` is updated in place.
 */
export async function registerAgentUnderIdentity(
  client: RegisterClient,
  params: { agentSessionName: string; sessionId?: string; datasetNames?: string[] },
  session: IdentitySession,
): Promise<{ ok: boolean; connectionId?: string }> {
  try {
    return await client.registerAgent(params);
  } catch (error) {
    const status = requestErrorStatus(error);
    if (!session.identity || (status !== 401 && status !== 403)) throw error;
    if (status === 403) {
      throw new PluginIdentityError(
        `The ${PLUGIN_KEY} identity was refused registration (403) by ${session.baseUrl}; owner fallback is disabled under pluginIdentity: true.`,
      );
    }
  }

  // 401 as the agent: the key is dead. Replace it once.
  await blockCachedIdentity(session.identity.apiKey, session.cachePath);
  session.identity = undefined;
  client.setApiKey(session.principalKey);
  const replacement = await ensurePluginIdentity(client, { ...session, mode: "enabled" });
  if (!replacement) throw new PluginIdentityError(`The ${PLUGIN_KEY} identity could not be replaced.`);
  session.identity = replacement;
  client.setApiKey(replacement.apiKey);
  try {
    return await client.registerAgent(params);
  } catch (error) {
    if (requestErrorStatus(error) === 401) {
      await blockCachedIdentity(replacement.apiKey, session.cachePath);
      throw new PluginIdentityError(
        `The ${PLUGIN_KEY} identity was rejected by ${session.baseUrl} and could not be replaced; owner fallback is disabled under pluginIdentity: true.`,
      );
    }
    throw error;
  }
}
