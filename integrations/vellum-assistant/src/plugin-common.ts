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
 * Shared state directory for cross-session state like the API key cache,
 * server-ready marker, and circuit breaker. Rooted under the plugin install
 * directory ($VELLUM_WORKSPACE_DIR/plugins/cognee/data) so state persists
 * across container restarts (Docker hatches only persist $VELLUM_WORKSPACE_DIR).
 * Falls back to ~/.cognee-plugin only when the workspace dir can't be
 * determined (e.g. running standalone outside a Vellum host).
 */
export function sharedStateDir(): string {
  const ws = workspaceDir();
  if (ws) return join(ws, "plugins", "cognee", "data");
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
 * `CogneePluginConfig.mode === "local"`. The init hook uses this to provision
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
   * Server mode. `local` = the plugin owns the server lifecycle (provisions
   * a venv, installs cognee, spawns uvicorn). `cloud` or `server` = an
   * externally managed server at `baseUrl`.
   *
   * Default (no config.json) is `local`. Supplying a config with a non-local
   * `base_url` flips this to `cloud`.
   */
  mode: "local" | "cloud" | "server";
  baseUrl: string;
  /**
   * Credential reference for the Cognee server base URL in `service:field` form
   * (e.g. `cognee:base_url`). Resolved via `assistant credentials reveal` at
   * runtime. When set, the plugin uses this URL instead of the local default,
   * auto-detecting cloud/server mode. This lets Option B users skip the
   * config.json step entirely.
   */
  baseUrlCredential: string;
  /**
   * Credential reference in `service:field` form (e.g. `cognee:api_key`).
   * Resolved at runtime via `assistant credentials reveal --service <s> --field <f>`.
   * Falls back to `COGNEE_API_KEY` env var or the auto-minted cache for local servers.
   */
  apiKeyCredential: string;
  /**
   * Credential reference for the LLM API key that the Cognee server needs
   * for its cognify pipeline (graph sync). In `service:field` form (e.g.
   * `openai:api_key`). Resolved via `assistant credentials reveal` and
   * passed to the managed server as `COGNEE_LLM_API_KEY`. For remote
   * servers, the LLM key must be configured on the server itself.
   */
  llmApiKeyCredential: string;
  dataset: string;
  agentName: string;
  sessionPrefix: string;
  autoImproveEvery: number;
  /** Local server spec; consulted only when `mode === "local"`. */
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
    env: { COGNEE_AGENT_MODE: "true", ENABLE_BACKEND_ACCESS_CONTROL: "false" },
  };
}

function defaultConfig(): CogneePluginConfig {
  const server = defaultServerSpec();
  return {
    mode: "local",
    baseUrl: `http://${server.host}:${server.port}`,
    baseUrlCredential: "cognee:base_url",
    apiKeyCredential: "cognee:api_key",
    llmApiKeyCredential: "cognee:llm_api_key",
    dataset: "agent_sessions",
    agentName: "vellum-assistant",
    sessionPrefix: "vellum",
    autoImproveEvery: 30,
    server,
  };
}

const DEFAULT_CONFIG: CogneePluginConfig = defaultConfig();

function configPath(): string {
  return join(getPluginRoot(), "config.json");
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
  takeString("base_url_credential", (v) => (cfg.baseUrlCredential = v));
  takeString("api_key_credential", (v) => (cfg.apiKeyCredential = v));
  takeString("llm_api_key_credential", (v) => (cfg.llmApiKeyCredential = v));
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
  if (process.env.COGNEE_MODE) {
    const m = process.env.COGNEE_MODE;
    if (m === "local" || m === "cloud" || m === "server") cfg.mode = m;
  }
  if (process.env.COGNEE_API_KEY_CREDENTIAL) cfg.apiKeyCredential = process.env.COGNEE_API_KEY_CREDENTIAL;
  if (process.env.COGNEE_BASE_URL_CREDENTIAL) cfg.baseUrlCredential = process.env.COGNEE_BASE_URL_CREDENTIAL;
  if (process.env.COGNEE_LLM_API_KEY_CREDENTIAL) cfg.llmApiKeyCredential = process.env.COGNEE_LLM_API_KEY_CREDENTIAL;
  if (process.env.COGNEE_PLUGIN_DATASET) cfg.dataset = process.env.COGNEE_PLUGIN_DATASET;
  if (process.env.COGNEE_AGENT_NAME) cfg.agentName = process.env.COGNEE_AGENT_NAME;
  if (process.env.COGNEE_SESSION_PREFIX) cfg.sessionPrefix = process.env.COGNEE_SESSION_PREFIX;
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
 * Default policy: no config supplied → `mode: "local"` (plugin manages the
 * server). A supplied config with a non-local `base_url` flips to `cloud`
 * unless it explicitly sets `mode`. In local mode, the base URL is always
 * derived from the server host/port so the two can't drift.
 */
export function validateConfig(raw: unknown): ValidatedConfig {
  const cfg = defaultConfig();
  const warnings: string[] = [];

  const provided =
    typeof raw === "object" &&
    raw !== null &&
    !Array.isArray(raw) &&
    Object.keys(raw as object).length > 0;

  let explicitMode = false;
  if (provided) {
    const seen = applyRawConfig(cfg, raw as Record<string, unknown>, warnings);
    explicitMode = seen.has("mode");
    // A supplied config defaults to cloud unless it explicitly set local mode.
    if (!explicitMode) cfg.mode = isLocalUrl(cfg.baseUrl) ? "local" : "cloud";
  }

  // Env overrides win over file/context.
  applyEnvOverrides(cfg);

  // For a local server, the base URL is owned by the server spec so the
  // reachability check and the spawned process always agree. An explicit
  // COGNEE_BASE_URL still wins (lets a local server bind a custom URL).
  if (cfg.mode === "local" && !process.env.COGNEE_BASE_URL && !process.env.COGNEE_LOCAL_API_URL) {
    cfg.baseUrl = `http://${cfg.server.host}:${cfg.server.port}`;
  }

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
      // The persisted file is the source of truth for mode once init has
      // written it. If it predates the mode field, derive from URL.
      if (!seen.has("mode")) cfg.mode = isLocalUrl(cfg.baseUrl) ? "local" : "cloud";
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
    mode: cfg.mode,
    base_url: cfg.baseUrl,
    base_url_credential: cfg.baseUrlCredential,
    api_key_credential: cfg.apiKeyCredential,
    llm_api_key_credential: cfg.llmApiKeyCredential,
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
 * Priority: 1. env var (COGNEE_API_KEY), 2. credential-resolved env var,
 * 3. cached key (minted on first init for local servers).
 *
 * The credential path works as follows: the config field `apiKeyCredential`
 * holds a `service:field` reference (e.g. `cognee:api_key`). The Vellum host
 * resolves this to an env var before the plugin hooks run. At runtime we
 * check the env var that the host would inject — `COGNEE_API_KEY` — which
 * is the same channel as a manually-set env var. This means the credential
 * store path and the manual env var path converge on the same resolution.
 */
export function resolveApiKey(baseUrl: string): string {
  const envKey = (process.env.COGNEE_API_KEY ?? "").trim();
  if (envKey) return envKey;
  return loadCachedApiKey(baseUrl);
}

// ─── Credential store resolution ──────────────────────────────────────────────

import { spawn } from "bun";

/**
 * Resolve a credential reference (e.g. `cognee:api_key`) to its plaintext
 * value using the `assistant credentials reveal` CLI. This is the actual
 * resolution path — the plugin calls the CLI directly rather than relying on
 * the host to inject env vars.
 *
 * Returns the plaintext value, or empty string if the credential is not found
 * or the CLI is unavailable. Never throws.
 */
async function resolveCredential(credentialRef: string): Promise<string> {
  if (!credentialRef || !credentialRef.includes(":")) return "";
  const [service, field] = credentialRef.split(":", 2);
  if (!service || !field) return "";

  try {
    const proc = spawn({
      cmd: ["assistant", "credentials", "reveal", "--service", service, "--field", field, "--json"],
      stdout: "pipe",
      stderr: "pipe",
    });
    const [stdout, , code] = await Promise.all([
      new Response(proc.stdout).text(),
      new Response(proc.stderr).text(),
      proc.exited,
    ]);
    if (code !== 0) return "";
    const data = JSON.parse(stdout) as Record<string, unknown>;
    if (data.ok === true) return String(data.value ?? "");
    return "";
  } catch {
    return "";
  }
}

/**
 * Resolve the API key using the full config, including the credential
 * reference. This is the preferred entry point for hooks that have a
 * loaded config.
 *
 * Resolution order:
 * 1. Credential store (via `assistant credentials reveal` if apiKeyCredential is set)
 * 2. COGNEE_API_KEY env var (manual override)
 * 3. Cached key (auto-minted on first init for local servers)
 */
export async function resolveApiKeyFromConfig(cfg: CogneePluginConfig): Promise<string> {
  if (cfg.apiKeyCredential) {
    const credKey = await resolveCredential(cfg.apiKeyCredential);
    if (credKey) return credKey;
  }
  return resolveApiKey(cfg.baseUrl);
}

/**
 * Resolve the Cognee server base URL from the credential store. If
 * `baseUrlCredential` is set (e.g. `cognee:base_url`) and the credential
 * resolves, the returned URL overrides the local default. This enables
 * the zero-config Option B flow: set a `cognee:base_url` credential and
 * the plugin auto-detects cloud mode without a config.json.
 *
 * Returns empty string if no credential is found.
 */
export async function resolveBaseUrl(cfg: CogneePluginConfig): Promise<string> {
  if (cfg.baseUrlCredential) {
    const credUrl = await resolveCredential(cfg.baseUrlCredential);
    if (credUrl) return credUrl.trim();
  }
  return "";
}

/**
 * Resolve the LLM API key for the Cognee server's cognify pipeline.
 * In local mode, this is passed to the spawned server process as
 * COGNEE_LLM_API_KEY. In cloud/server mode, the LLM key must be
 * configured on the server itself.
 *
 * Resolution order:
 * 1. Credential store (via `assistant credentials reveal` if llmApiKeyCredential is set)
 * 2. COGNEE_LLM_API_KEY env var (manual override)
 */
export async function resolveLlmApiKey(cfg: CogneePluginConfig): Promise<string> {
  if (cfg.llmApiKeyCredential) {
    const credKey = await resolveCredential(cfg.llmApiKeyCredential);
    if (credKey) return credKey;
  }
  return (process.env.COGNEE_LLM_API_KEY ?? "").trim();
}

/**
 * Resolve the HTTP endpoint (baseUrl + apiKey) for runtime calls.
 */
export async function resolveHttpEndpoint(): Promise<{ baseUrl: string; apiKey: string }> {
    const cfg = loadConfig();
    let baseUrl = cfg.baseUrl.replace(/\/+$/, "");
    // If the config has the default local base URL, check the credential
    // store for a base_url override (Option B zero-config flow).
    if (cfg.baseUrlCredential && isLocalUrl(baseUrl)) {
      const credUrl = await resolveBaseUrl(cfg);
      if (credUrl) baseUrl = credUrl.replace(/\/+$/, "");
    }
    const apiKey = await resolveApiKeyFromConfig(cfg);
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
 * Records the PID of a plugin-spawned (local mode) Cognee server so the shutdown
 * hook can tear it down. Only written when the plugin owns the server
 * lifecycle (`config.mode === "local"`).
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
