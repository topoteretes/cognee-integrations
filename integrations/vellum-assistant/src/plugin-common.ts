/**
 * Shared helpers across all plugin modules.
 *
 * Ported from _plugin_common.py and config.py. Provides:
 *   - Config loading (file + env vars + defaults)
 *   - Session ID resolution and mapping
 *   - Hook logging to disk
 *   - File-based state management
 *   - API key resolution and caching
 *   - Plugin directory resolution
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join, dirname, basename } from "node:path";
import { createHash, randomUUID } from "node:crypto";

// ─── Plugin directory resolution ──────────────────────────────────────────────

/**
 * The plugin's root directory. At runtime this is set by the bridge from
 * VELLUM_PLUGIN_ROOT or derived from the module URL. In Vellum's plugin
 * layout, plugins live at $VELLUM_WORKSPACE_DIR/plugins/<name>.
 */
let pluginRoot = process.env.VELLUM_PLUGIN_ROOT ?? "";

export function setPluginRoot(root: string): void {
  pluginRoot = root;
}

export function getPluginRoot(): string {
  if (pluginRoot) return pluginRoot;
  // Derive from this module's URL.
  try {
    const url = import.meta.url;
    const path = url.replace(/^file:\/\//, "");
    pluginRoot = join(dirname(path), "..");
  } catch {
    pluginRoot = process.cwd();
  }
  return pluginRoot;
}

/**
 * Shared state directory (~/.cognee-plugin) for cross-session state like
 * the API key cache, server-ready marker, and circuit breaker.
 */
export function sharedStateDir(): string {
  return process.env.COGNEE_PLUGIN_STATE_DIR ?? join(homedir(), ".cognee-plugin");
}

/**
 * Per-plugin state directory for vellum-assistant specifically.
 */
export function pluginStateDir(): string {
  return join(sharedStateDir(), "vellum-assistant");
}

/**
 * The workspace directory, derived from VELLUM_WORKSPACE_DIR or from the
 * plugin storage dir (up two levels from plugins-data/<name>).
 */
export function workspaceDir(): string {
  if (process.env.VELLUM_WORKSPACE_DIR) return process.env.VELLUM_WORKSPACE_DIR;
  // Fallback: pluginStorageDir is <workspace>/plugins-data/<plugin>, so up 2.
  if (process.env.VELLUM_PLUGIN_STORAGE_DIR) {
    return join(process.env.VELLUM_PLUGIN_STORAGE_DIR, "..", "..");
  }
  return "";
}

// ─── Config ───────────────────────────────────────────────────────────────────

/**
 * Spec for a locally managed Cognee server. Only meaningful when
 * `CogneePluginConfig.managed === true`. The init hook uses this to provision
 * a venv (if missing) and spawn the server process.
 */
export interface CogneeServerSpec {
  /**
   * Python interpreter used to create the venv. May be a bare command
   * resolved from PATH ("python3") or an absolute path. The server itself
   * runs from the venv's own interpreter, not this one.
   */
  python: string;
  /** Absolute path to the venv the plugin creates and runs the server from. */
  venvDir: string;
  /** Host the local server binds to. */
  host: string;
  /** Port the local server listens on. */
  port: number;
  /** Extra environment variables passed to the server process. */
  env: Record<string, string>;
}

export interface CogneePluginConfig {
  /**
   * Whether the plugin owns the Cognee server lifecycle. When true the init
   * hook provisions + spawns a local server from `server`; when false the
   * plugin assumes an externally managed (remote) server at `baseUrl`.
   *
   * Default (no config.json) is `true` — a local, assistant-managed server.
   * Supplying a config.json flips the default to `false` (remote) unless the
   * config explicitly sets `managed`.
   */
  managed: boolean;
  mode: "local" | "cloud" | "server";
  baseUrl: string;
  apiKey: string;
  dataset: string;
  agentName: string;
  sessionPrefix: string;
  autoImproveEvery: number;
  /** Local managed-server spec; consulted only when `managed === true`. */
  server: CogneeServerSpec;
}

const DEFAULT_SERVER_HOST = "127.0.0.1";
const DEFAULT_SERVER_PORT = 8011;

function defaultServerSpec(): CogneeServerSpec {
  return {
    python: "python3",
    venvDir: join(sharedStateDir(), "server-venv"),
    host: DEFAULT_SERVER_HOST,
    port: DEFAULT_SERVER_PORT,
    env: { COGNEE_AGENT_MODE: "true" },
  };
}

function defaultConfig(): CogneePluginConfig {
  const server = defaultServerSpec();
  return {
    managed: true,
    mode: "local",
    baseUrl: `http://${server.host}:${server.port}`,
    apiKey: "",
    dataset: "agent_sessions",
    agentName: "vellum-assistant",
    sessionPrefix: "vellum",
    autoImproveEvery: 30,
    server,
  };
}

const DEFAULT_CONFIG: CogneePluginConfig = defaultConfig();

function configPath(): string {
  return join(pluginStateDir(), "config.json");
}

/**
 * Overlay a raw (snake_case, untrusted) object onto a config in place,
 * coercing + validating each field. Unknown/wrong-typed fields are skipped
 * and a human-readable note is pushed to `warnings` so callers can surface
 * what was ignored. Returns the set of top-level keys that were actually
 * present in `raw` (so callers can tell "provided" from "defaulted").
 */
function applyRawConfig(
  cfg: CogneePluginConfig,
  raw: Record<string, unknown>,
  warnings: string[],
): Set<string> {
  const seen = new Set<string>();

  const takeString = (key: string, apply: (v: string) => void) => {
    if (!(key in raw)) return;
    seen.add(key);
    const v = raw[key];
    if (typeof v === "string" && v.trim()) apply(v.trim());
    else warnings.push(`"${key}" must be a non-empty string — ignoring`);
  };

  takeString("base_url", (v) => (cfg.baseUrl = v));
  takeString("api_key", (v) => (cfg.apiKey = v));
  takeString("dataset", (v) => (cfg.dataset = v));
  takeString("agent_name", (v) => (cfg.agentName = v));
  takeString("session_prefix", (v) => (cfg.sessionPrefix = v));

  if ("mode" in raw) {
    seen.add("mode");
    const v = raw.mode;
    if (v === "local" || v === "cloud" || v === "server") cfg.mode = v;
    else warnings.push(`"mode" must be one of local|cloud|server — ignoring`);
  }

  if ("auto_improve_every" in raw) {
    seen.add("auto_improve_every");
    const n = Number(raw.auto_improve_every);
    if (Number.isFinite(n) && n > 0) cfg.autoImproveEvery = n;
    else warnings.push(`"auto_improve_every" must be a positive number — ignoring`);
  }

  if ("managed" in raw) {
    seen.add("managed");
    const v = raw.managed;
    if (typeof v === "boolean") cfg.managed = v;
    else warnings.push(`"managed" must be a boolean — ignoring`);
  }

  if ("server" in raw) {
    seen.add("server");
    const s = raw.server;
    if (typeof s === "object" && s !== null && !Array.isArray(s)) {
      const sv = s as Record<string, unknown>;
      if (typeof sv.python === "string" && sv.python.trim()) cfg.server.python = sv.python.trim();
      if (typeof sv.venv_dir === "string" && sv.venv_dir.trim()) cfg.server.venvDir = sv.venv_dir.trim();
      if (typeof sv.host === "string" && sv.host.trim()) cfg.server.host = sv.host.trim();
      if (sv.port !== undefined) {
        const p = Number(sv.port);
        if (Number.isInteger(p) && p > 0 && p < 65536) cfg.server.port = p;
        else warnings.push(`"server.port" must be a valid port — ignoring`);
      }
      if (sv.env !== undefined) {
        if (typeof sv.env === "object" && sv.env !== null && !Array.isArray(sv.env)) {
          const env: Record<string, string> = {};
          for (const [k, val] of Object.entries(sv.env as Record<string, unknown>)) {
            env[k] = String(val);
          }
          cfg.server.env = { ...cfg.server.env, ...env };
        } else {
          warnings.push(`"server.env" must be an object — ignoring`);
        }
      }
    } else {
      warnings.push(`"server" must be an object — ignoring`);
    }
  }

  return seen;
}

/**
 * Apply environment-variable overrides (highest priority) onto a config.
 */
function applyEnvOverrides(cfg: CogneePluginConfig): void {
  if (process.env.COGNEE_BASE_URL) cfg.baseUrl = process.env.COGNEE_BASE_URL;
  if (process.env.COGNEE_LOCAL_API_URL && !process.env.COGNEE_BASE_URL) {
    cfg.baseUrl = process.env.COGNEE_LOCAL_API_URL;
  }
  if (process.env.COGNEE_API_KEY) cfg.apiKey = process.env.COGNEE_API_KEY;
  if (process.env.COGNEE_PLUGIN_DATASET) cfg.dataset = process.env.COGNEE_PLUGIN_DATASET;
  if (process.env.COGNEE_AGENT_NAME) cfg.agentName = process.env.COGNEE_AGENT_NAME;
  if (process.env.COGNEE_SESSION_PREFIX) cfg.sessionPrefix = process.env.COGNEE_SESSION_PREFIX;
  if (process.env.COGNEE_MANAGED) {
    cfg.managed = process.env.COGNEE_MANAGED === "true" || process.env.COGNEE_MANAGED === "1";
  }
}

/**
 * Result of validating the host-supplied init config (`InitContext.config`).
 */
export interface ValidatedConfig {
  config: CogneePluginConfig;
  /** True when a non-empty config object was supplied by the host. */
  fromContext: boolean;
  /** Human-readable notes about fields that were ignored or coerced. */
  warnings: string[];
}

/**
 * Validate the config the init hook receives from `InitContext.config`.
 *
 * The host parses `<pluginDir>/config.json` and hands it over as `unknown`.
 * This is the authoritative config channel; `loadConfig()` reads the plugin's
 * own persisted state file for runtime hooks.
 *
 * Default policy: no config supplied → a local, assistant-managed server
 * (`managed: true`). A supplied config flips the default to remote
 * (`managed: false`) unless it explicitly sets `managed`. When managed, the
 * base URL is always derived from the server host/port so the two can't drift.
 */
export function validateConfig(raw: unknown): ValidatedConfig {
  const cfg = defaultConfig();
  const warnings: string[] = [];

  const provided =
    typeof raw === "object" &&
    raw !== null &&
    !Array.isArray(raw) &&
    Object.keys(raw as object).length > 0;

  let explicitManaged = false;
  if (provided) {
    const seen = applyRawConfig(cfg, raw as Record<string, unknown>, warnings);
    explicitManaged = seen.has("managed");
    // A supplied config defaults to remote unless it asked to be managed.
    if (!explicitManaged) cfg.managed = false;
  }

  // Env overrides win over file/context.
  applyEnvOverrides(cfg);
  if (process.env.COGNEE_MANAGED) explicitManaged = true;

  // For a managed server, the base URL is owned by the server spec so the
  // reachability check and the spawned process always agree. An explicit
  // COGNEE_BASE_URL still wins (lets a managed server bind a custom URL).
  if (cfg.managed && !process.env.COGNEE_BASE_URL && !process.env.COGNEE_LOCAL_API_URL) {
    cfg.baseUrl = `http://${cfg.server.host}:${cfg.server.port}`;
  }

  cfg.mode = isLocalUrl(cfg.baseUrl) ? "local" : "cloud";

  return { config: cfg, fromContext: provided, warnings };
}

export function loadConfig(): CogneePluginConfig {
  const cfg = defaultConfig();

  // 1. Config file
  try {
    const data = JSON.parse(readFileSync(configPath(), "utf-8"));
    if (typeof data === "object" && data !== null && !Array.isArray(data)) {
      const warnings: string[] = [];
      const seen = applyRawConfig(cfg, data as Record<string, unknown>, warnings);
      // The persisted file is the source of truth for managed-ness once init
      // has written it. If it predates the managed field, fall back to URL.
      if (!seen.has("managed")) cfg.managed = isLocalUrl(cfg.baseUrl);
    }
  } catch {
    // No config file yet — write defaults so it exists for future reads.
    try {
      mkdirSync(pluginStateDir(), { recursive: true });
      writeFileSync(configPath(), JSON.stringify(DEFAULT_CONFIG, null, 2), "utf-8");
    } catch {
      // Best-effort — don't fail if the dir isn't writable.
    }
  }

  // 2. Env var overrides (higher priority)
  applyEnvOverrides(cfg);

  return cfg;
}

/**
 * Serialize a config to the snake_case shape on disk that `loadConfig` /
 * `applyRawConfig` read back. Keeps persistence a clean round-trip.
 */
function toRawConfig(cfg: CogneePluginConfig): Record<string, unknown> {
  return {
    managed: cfg.managed,
    mode: cfg.mode,
    base_url: cfg.baseUrl,
    api_key: cfg.apiKey,
    dataset: cfg.dataset,
    agent_name: cfg.agentName,
    session_prefix: cfg.sessionPrefix,
    auto_improve_every: cfg.autoImproveEvery,
    server: {
      python: cfg.server.python,
      venv_dir: cfg.server.venvDir,
      host: cfg.server.host,
      port: cfg.server.port,
      env: cfg.server.env,
    },
  };
}

export function saveConfig(cfg: Partial<CogneePluginConfig>): void {
  try {
    const dir = pluginStateDir();
    mkdirSync(dir, { recursive: true });
    const existing = loadConfig();
    const merged: CogneePluginConfig = {
      ...existing,
      ...cfg,
      server: { ...existing.server, ...(cfg.server ?? {}) },
    };
    writeFileSync(configPath(), JSON.stringify(toRawConfig(merged), null, 2), "utf-8");
  } catch {
    // Best-effort.
  }
}

/**
 * Determine if a URL is a loopback/local address.
 */
export function isLocalUrl(url: string): boolean {
  try {
    const u = new URL(url);
    const hostname = u.hostname;
    return ["localhost", "127.0.0.1", "::1", ""].includes(hostname);
  } catch {
    return true;
  }
}

/**
 * Determine the active mode from the base URL.
 */
export function resolveMode(baseUrl: string): "local" | "cloud" {
  return isLocalUrl(baseUrl) ? "local" : "cloud";
}

// ─── Session key management ───────────────────────────────────────────────────

/**
 * Sanitize a string for use as a session key (alphanumeric + -_. only).
 */
export function sanitizeSessionKey(value: string): string {
  return value
    .replace(/[^a-zA-Z0-9\-_.]/g, "_")
    .replace(/^[._]+|[._]+$/g, "")
    .slice(0, 120);
}

/**
 * Get the session key from the env var (set by hooks from ctx.conversationId).
 */
export function getSessionKey(): string {
  const raw = process.env.COGNEE_SESSION_KEY ?? "";
  return sanitizeSessionKey(raw.trim());
}

/**
 * Build the Cognee session ID from the agent name and host session key.
 * Format: {agentName}_{hostSessionKey}
 */
export function buildSessionId(agentName: string, hostKey: string): string {
  return `${agentName}_${hostKey}`;
}

// ─── Session map (host key → Cognee session) ──────────────────────────────────

function sessionsDir(): string {
  return join(pluginStateDir(), "sessions");
}

function sessionMapPath(hostKey: string): string {
  return join(sessionsDir(), `${hostKey}.json`);
}

interface SessionMapRecord {
  session_id: string;
  conn_uuid: string;
  host_key: string;
  created_at: number;
}

/**
 * Resolve or create the Cognee session ID for a given host session key.
 * Uses first-writer-wins (O_CREAT|O_EXCL equivalent) so concurrent hooks
 * all resolve the same session.
 */
export function resolveSessionId(hostKey: string, agentName: string): string {
  const sanitized = sanitizeSessionKey(hostKey);
  if (!sanitized) return "";

  try {
    mkdirSync(sessionsDir(), { recursive: true });
    const path = sessionMapPath(sanitized);

    // Try to read existing.
    if (existsSync(path)) {
      const record = JSON.parse(readFileSync(path, "utf-8")) as SessionMapRecord;
      if (record.session_id) return record.session_id;
    }

    // First writer — create the record.
    const sessionId = buildSessionId(agentName, sanitized);
    const record: SessionMapRecord = {
      session_id: sessionId,
      conn_uuid: randomUUID(),
      host_key: sanitized,
      created_at: Date.now(),
    };
    writeFileSync(path, JSON.stringify(record, null, 2), "utf-8");
    return sessionId;
  } catch {
    return buildSessionId(agentName, sanitized);
  }
}

/**
 * Get the connection UUID for a given host session key.
 */
export function getConnUuid(hostKey: string): string {
  const sanitized = sanitizeSessionKey(hostKey);
  try {
    const path = sessionMapPath(sanitized);
    if (existsSync(path)) {
      const record = JSON.parse(readFileSync(path, "utf-8")) as SessionMapRecord;
      return record.conn_uuid ?? "";
    }
  } catch {
    // Fall through.
  }
  return "";
}

// ─── API key resolution ───────────────────────────────────────────────────────

function apiKeyCachePath(): string {
  return join(sharedStateDir(), "api_key.json");
}

interface ApiKeyCache {
  api_key: string;
  base_url: string;
  created_at: number;
}

/**
 * Load the cached API key (single-principal model).
 */
export function loadCachedApiKey(baseUrl: string): string {
  try {
    const cache = JSON.parse(readFileSync(apiKeyCachePath(), "utf-8")) as ApiKeyCache;
    if (cache.api_key) {
      // If the cached URL matches (or no URL in cache), use the key.
      if (!cache.base_url || cache.base_url.replace(/\/+$/, "") === baseUrl.replace(/\/+$/, "")) {
        return cache.api_key;
      }
    }
  } catch {
    // No cache yet.
  }
  return "";
}

/**
 * Cache the API key for future use.
 */
export function cacheApiKey(apiKey: string, baseUrl: string): void {
  try {
    const dir = sharedStateDir();
    mkdirSync(dir, { recursive: true });
    const cache: ApiKeyCache = {
      api_key: apiKey,
      base_url: baseUrl.replace(/\/+$/, ""),
      created_at: Date.now(),
    };
    writeFileSync(apiKeyCachePath(), JSON.stringify(cache, null, 2), "utf-8");
  } catch {
    // Best-effort.
  }
}

/**
 * Resolve the API key for HTTP calls.
 * Priority: 1. env var, 2. cached key.
 */
export function resolveApiKey(baseUrl: string): string {
  const envKey = (process.env.COGNEE_API_KEY ?? "").trim();
  if (envKey) return envKey;
  return loadCachedApiKey(baseUrl);
}

/**
 * Resolve the HTTP endpoint (baseUrl + apiKey) for runtime calls.
 */
export function resolveHttpEndpoint(): { baseUrl: string; apiKey: string } {
  const cfg = loadConfig();
  const baseUrl = cfg.baseUrl.replace(/\/+$/, "");
  const apiKey = resolveApiKey(baseUrl);
  return { baseUrl, apiKey };
}

// ─── Hook logging ─────────────────────────────────────────────────────────────

const LOG_LINE_CAP = 600;

export function hookLog(event: string, detail?: Record<string, unknown>): void {
  try {
    const dir = pluginStateDir();
    mkdirSync(dir, { recursive: true });
    const line: Record<string, unknown> = {
      ts: new Date().toISOString(),
      pid: process.pid,
      event,
    };
    if (detail) {
      // Cap detail values to avoid log bloat.
      const capped: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(detail)) {
        capped[key] = typeof value === "string" ? value.slice(0, LOG_LINE_CAP) : value;
      }
      line.detail = capped;
    }
    writeFileSync(
      join(dir, "hook.log"),
      JSON.stringify(line) + "\n",
      { flag: "a", encoding: "utf-8" },
    );
  } catch {
    // Best-effort — never throw from logging.
  }
}

// ─── Activity tracking ────────────────────────────────────────────────────────

/**
 * Touch the activity file so the idle watcher knows we're alive.
 */
export function touchActivity(): void {
  try {
    const dir = pluginStateDir();
    mkdirSync(dir, { recursive: true });
    const now = Date.now() / 1000;
    writeFileSync(join(dir, "activity.ts"), String(now), "utf-8");
  } catch {
    // Best-effort.
  }
}

// ─── Save counter ─────────────────────────────────────────────────────────────

export const SAVE_KINDS = ["prompt", "trace", "answer"] as const;
type SaveKind = (typeof SAVE_KINDS)[number];

function saveCounterPath(): string {
  return join(pluginStateDir(), "save_counter.json");
}

interface SaveCounter {
  count: number;
  last_improve: number;
  kinds: Record<SaveKind, number>;
}

function readSaveCounter(): SaveCounter {
  try {
    return JSON.parse(readFileSync(saveCounterPath(), "utf-8"));
  } catch {
    return { count: 0, last_improve: 0, kinds: { prompt: 0, trace: 0, answer: 0 } };
  }
}

function writeSaveCounter(state: SaveCounter): void {
  try {
    mkdirSync(pluginStateDir(), { recursive: true });
    writeFileSync(saveCounterPath(), JSON.stringify(state), "utf-8");
  } catch {
    // Best-effort.
  }
}

/**
 * Bump the save counter and return whether it's time to auto-improve
 * (bridge session cache to graph).
 */
export function bumpSaveCounter(kind: SaveKind, threshold?: number): boolean {
  const cfg = loadConfig();
  const limit = threshold ?? cfg.autoImproveEvery;
  const state = readSaveCounter();
  state.count += 1;
  state.kinds[kind] = (state.kinds[kind] ?? 0) + 1;
  const shouldImprove = state.count - state.last_improve >= limit;
  if (shouldImprove) {
    state.last_improve = state.count;
  }
  writeSaveCounter(state);
  return shouldImprove;
}

// ─── Pending prompts (for pairing on Stop) ────────────────────────────────────

function pendingDir(): string {
  return join(pluginStateDir(), "pending");
}

function pendingPromptPath(sessionKey: string): string {
  return join(pendingDir(), `${sessionKey}.prompt.json`);
}

export interface PendingPrompt {
  prompt: string;
  timestamp: number;
  cwd: string;
}

export function stagePendingPrompt(sessionKey: string, prompt: string, cwd: string): void {
  try {
    mkdirSync(pendingDir(), { recursive: true });
    const entry: PendingPrompt = { prompt, timestamp: Date.now(), cwd };
    writeFileSync(pendingPromptPath(sessionKey), JSON.stringify(entry), "utf-8");
  } catch {
    // Best-effort.
  }
}

export function consumePendingPrompt(sessionKey: string): PendingPrompt | null {
  try {
    const path = pendingPromptPath(sessionKey);
    if (!existsSync(path)) return null;
    const entry = JSON.parse(readFileSync(path, "utf-8")) as PendingPrompt;
    // Delete after reading.
    try {
      writeFileSync(path, "", "utf-8");
    } catch {
      // Best-effort.
    }
    return entry;
  } catch {
    return null;
  }
}

// ─── Bridge cache (session cache shadow for HTTP-mode graph sync) ─────────────

function bridgeDir(): string {
  return join(pluginStateDir(), "bridge");
}

function bridgeFilePath(sessionId: string): string {
  return join(bridgeDir(), `${sessionId}.json`);
}

interface SessionBridgeEntry {
  qa?: Array<{ question: string; answer: string }>;
  trace?: string[];
}

/**
 * On-disk bridge cache. Session entries are keyed by `dataset:sessionId`; the
 * reserved `_state` key holds posted-document dedup hashes. Both live in one
 * flat map on disk, so the index signature carries their union.
 */
interface BridgeCache {
  [key: string]: SessionBridgeEntry | Record<string, string> | undefined;
  _state?: Record<string, string>;
}

function bridgeCacheKey(dataset: string, sessionId: string): string {
  return `${dataset}:${sessionId}`;
}

/**
 * Resolve (creating if absent) the session entry for a `dataset:sessionId`
 * key. Returns a live reference into `cache`, so mutations persist on write.
 */
function getSessionEntry(cache: BridgeCache, key: string): SessionBridgeEntry {
  let entry = cache[key] as SessionBridgeEntry | undefined;
  if (!entry) {
    entry = {};
    cache[key] = entry;
  }
  return entry;
}

function loadBridgeFile(sessionId: string): BridgeCache {
  try {
    return JSON.parse(readFileSync(bridgeFilePath(sessionId), "utf-8"));
  } catch {
    return {};
  }
}

function writeBridgeFile(sessionId: string, cache: BridgeCache): void {
  try {
    mkdirSync(bridgeDir(), { recursive: true });
    writeFileSync(bridgeFilePath(sessionId), JSON.stringify(cache, null, 2), "utf-8");
  } catch {
    // Best-effort.
  }
}

/**
 * Record a QA entry in the bridge cache.
 */
export function recordBridgeQA(
  sessionId: string,
  dataset: string,
  question: string,
  answer: string,
): void {
  const cache = loadBridgeFile(sessionId);
  const entry = getSessionEntry(cache, bridgeCacheKey(dataset, sessionId));
  if (!entry.qa) entry.qa = [];
  entry.qa.push({ question, answer });
  writeBridgeFile(sessionId, cache);
}

/**
 * Record a trace entry in the bridge cache.
 */
export function recordBridgeTrace(
  sessionId: string,
  dataset: string,
  traceText: string,
): void {
  const cache = loadBridgeFile(sessionId);
  const entry = getSessionEntry(cache, bridgeCacheKey(dataset, sessionId));
  if (!entry.trace) entry.trace = [];
  entry.trace.push(traceText);
  writeBridgeFile(sessionId, cache);
}

/**
 * Format the cached bridge document for posting to the graph.
 * Returns [qaDoc, traceDoc] as text.
 */
export function formatBridgeDocument(
  sessionId: string,
  dataset: string,
): [string, string] {
  const cache = loadBridgeFile(sessionId);
  const key = bridgeCacheKey(dataset, sessionId);
  const sessionCache = (cache[key] as SessionBridgeEntry | undefined) ?? {};

  const qaLines: string[] = [];
  for (const entry of sessionCache.qa ?? []) {
    if (entry.question) qaLines.push(`Question: ${entry.question}`);
    if (entry.answer) qaLines.push(`Answer: ${entry.answer}`);
    if (entry.question || entry.answer) qaLines.push("");
  }

  const traceLines = (sessionCache.trace ?? []).filter((t) => t.trim());

  let qaDoc = qaLines.join("\n").trim();
  let traceDoc = traceLines.join("\n\n").trim();
  if (qaDoc) qaDoc = `Session ID: ${sessionId}\n\n${qaDoc}`;
  if (traceDoc) traceDoc = `Session ID: ${sessionId}\n\n${traceDoc}`;
  return [qaDoc, traceDoc];
}

/**
 * Mark a bridge document as posted to the graph (dedup by content hash).
 */
export function markBridgePosted(
  sessionId: string,
  dataset: string,
  kind: string,
  document: string,
): void {
  const cache = loadBridgeFile(sessionId);
  if (!cache._state) cache._state = {};
  const stateKey = `${bridgeCacheKey(dataset, sessionId)}:${kind}`;
  cache._state[stateKey] = createHash("sha256").update(document).digest("hex");
  writeBridgeFile(sessionId, cache);
}

/**
 * Check if a bridge document has already been posted (same content hash).
 */
export function isBridgePosted(
  sessionId: string,
  dataset: string,
  kind: string,
  document: string,
): boolean {
  const cache = loadBridgeFile(sessionId);
  if (!cache._state) return false;
  const stateKey = `${bridgeCacheKey(dataset, sessionId)}:${kind}`;
  const digest = createHash("sha256").update(document).digest("hex");
  return cache._state[stateKey] === digest;
}

// ─── Server-ready marker ──────────────────────────────────────────────────────

function serverReadyPath(): string {
  return join(sharedStateDir(), "server-ready.json");
}

export function markServerReady(): void {
  try {
    const dir = sharedStateDir();
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      serverReadyPath(),
      JSON.stringify({ ready: true, ts: Date.now() }),
      "utf-8",
    );
  } catch {
    // Best-effort.
  }
}

export function clearServerReady(): void {
  try {
    const path = serverReadyPath();
    if (existsSync(path)) {
      writeFileSync(path, "", "utf-8");
    }
  } catch {
    // Best-effort.
  }
}

export function isServerReady(): boolean {
  try {
    const data = JSON.parse(readFileSync(serverReadyPath(), "utf-8"));
    if (!data.ready) return false;
    // TTL of 30 seconds.
    const age = Date.now() - (data.ts ?? 0);
    return age < 30_000;
  } catch {
    return false;
  }
}

// ─── Managed-server PID tracking ──────────────────────────────────────────────

/**
 * Records the PID of a plugin-spawned (managed) Cognee server so the shutdown
 * hook can tear it down. Only written when the plugin owns the server
 * lifecycle (`config.managed === true`).
 */
function serverPidPath(): string {
  return join(sharedStateDir(), "server.pid");
}

export function writeServerPid(pid: number): void {
  try {
    mkdirSync(sharedStateDir(), { recursive: true });
    writeFileSync(serverPidPath(), JSON.stringify({ pid, ts: Date.now() }), "utf-8");
  } catch {
    // Best-effort.
  }
}

export function readServerPid(): number | null {
  try {
    const data = JSON.parse(readFileSync(serverPidPath(), "utf-8"));
    const pid = Number(data.pid);
    return Number.isInteger(pid) && pid > 0 ? pid : null;
  } catch {
    return null;
  }
}

export function clearServerPid(): void {
  try {
    const path = serverPidPath();
    if (existsSync(path)) writeFileSync(path, "", "utf-8");
  } catch {
    // Best-effort.
  }
}

/**
 * Path the managed server's stdout/stderr is redirected to.
 */
export function serverLogPath(): string {
  return join(pluginStateDir(), "server.log");
}

// ─── Git branch detection (for session IDs) ───────────────────────────────────

import { spawn } from "bun";

export async function detectGitBranch(cwd: string): Promise<string> {
  try {
    const proc = spawn({
      cmd: ["git", "rev-parse", "--abbrev-ref", "HEAD"],
      cwd,
      stdout: "pipe",
      stderr: "pipe",
    });
    const stdout = await new Response(proc.stdout).text();
    const exitCode = await proc.exited;
    if (exitCode === 0) {
      return stdout.trim().replace(/[/\s]/g, "-").slice(0, 40);
    }
  } catch {
    // Not a git repo or git not available.
  }
  return "";
}

// ─── Memory preference (system message injection) ────────────────────────────

/**
 * Build the memory preference steer text that gets injected into the system
 * message on session start. Tells the assistant to prefer Cognee memory.
 */
export function memoryPreferenceSteer(): string {
  return [
    "## Cognee Memory Active",
    "",
    "Long-term memory is powered by Cognee. The assistant's built-in memory",
    "system has been disabled in favor of Cognee's knowledge graph.",
    "",
    "- Relevant context from prior sessions is automatically injected before each response.",
    "- Use the cognee_recall tool for deeper or cross-session searches.",
    "- Session interactions (prompts, tool calls, responses) are stored automatically.",
    "- Use the cognee-remember skill to ingest new information into the permanent graph.",
    "- Use the cognee-sync skill to bridge session data into the permanent graph.",
  ].join("\n");
}
