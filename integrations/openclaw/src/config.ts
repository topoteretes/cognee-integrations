import type { CogneeMode, CogneePluginConfig, CogneeSearchType, MemoryScope, ScopeRoute } from "./types.js";

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

export const DEFAULT_BASE_URL = "http://localhost:8000";
export const DEFAULT_DATASET_NAME = "openclaw";
export const DEFAULT_SEARCH_TYPE: CogneeSearchType = "GRAPH_COMPLETION";
export const DEFAULT_DELETE_MODE = "soft" as const;
export const DEFAULT_MAX_RESULTS = 3;
export const DEFAULT_MIN_SCORE = 0.3;
export const DEFAULT_MAX_TOKENS = 512;
export const DEFAULT_RECALL_INJECTION_POSITION = "prependContext" as const;
export const DEFAULT_AUTO_RECALL = true;
export const DEFAULT_AUTO_INDEX = true;
export const DEFAULT_AUTO_COGNIFY = true;
export const DEFAULT_AUTO_MEMIFY = false;
export const DEFAULT_IMPROVE_ON_SESSION_END = true;
export const DEFAULT_REQUEST_TIMEOUT_MS = 60_000;
export const DEFAULT_INGESTION_TIMEOUT_MS = 300_000;

export const DEFAULT_RECALL_SCOPES: MemoryScope[] = ["agent", "user", "company"];
export const DEFAULT_WRITE_SCOPE: MemoryScope = "agent";
export const DEFAULT_SCOPE_ROUTING: ScopeRoute[] = [
  { pattern: "memory/company/**", scope: "company" },
  { pattern: "memory/company/*", scope: "company" },
  { pattern: "memory/user/**", scope: "user" },
  { pattern: "memory/user/*", scope: "user" },
  { pattern: "memory/**", scope: "agent" },
  { pattern: "memory/*", scope: "agent" },
  { pattern: "MEMORY.md", scope: "agent" },
];

/** Glob patterns for memory files, relative to workspace root. */
export const MEMORY_FILE_PATTERNS = ["MEMORY.md", "memory"];

const VALID_RECALL_POSITIONS = [
  "prependSystemContext",
  "appendSystemContext",
  "prependContext",
] as const;

const VALID_WRITE_SCOPES: MemoryScope[] = ["company", "user", "agent"];

// ---------------------------------------------------------------------------
// Env var resolution
// ---------------------------------------------------------------------------

export function resolveEnvVars(value: string): string {
  return value.replace(/\$\{([^}]+)\}/g, (_, envVar) => {
    const envValue = process.env[envVar];
    if (!envValue) {
      throw new Error(`Environment variable ${envVar} is not set`);
    }
    return envValue;
  });
}

function envTrim(key: string): string | undefined {
  const value = process.env[key]?.trim();
  return value ? value : undefined;
}

function pickString(options: {
  envKeys?: string | string[];
  file?: string | undefined;
  defaultValue: string;
}): string {
  const keys = options.envKeys
    ? Array.isArray(options.envKeys)
      ? options.envKeys
      : [options.envKeys]
    : [];
  for (const key of keys) {
    const env = envTrim(key);
    if (env !== undefined) return env;
  }
  const file = options.file?.trim();
  if (file) return file;
  return options.defaultValue;
}

function pickOptionalString(options: {
  envKeys?: string | string[];
  file?: string | undefined;
}): string {
  const keys = options.envKeys
    ? Array.isArray(options.envKeys)
      ? options.envKeys
      : [options.envKeys]
    : [];
  for (const key of keys) {
    const env = envTrim(key);
    if (env !== undefined) return env;
  }
  return options.file?.trim() || "";
}

function pickBool(options: {
  envKey?: string;
  file?: boolean | undefined;
  defaultValue: boolean;
}): boolean {
  if (options.envKey) {
    const env = process.env[options.envKey];
    if (env !== undefined && env !== "") {
      return env === "true" || env === "1";
    }
  }
  if (typeof options.file === "boolean") return options.file;
  return options.defaultValue;
}

function pickNumber(options: {
  envKey?: string;
  file?: number | undefined;
  defaultValue: number;
}): number {
  if (options.envKey) {
    const env = process.env[options.envKey];
    if (env !== undefined && env !== "") {
      const parsed = Number(env);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  if (typeof options.file === "number") return options.file;
  return options.defaultValue;
}

function pickMode(raw: CogneePluginConfig): CogneeMode {
  const env = envTrim("COGNEE_MODE");
  if (env === "cloud" || env === "local") return env;
  return raw.mode === "cloud" ? "cloud" : "local";
}

function pickDeleteMode(raw: CogneePluginConfig): "soft" | "hard" {
  const env = envTrim("COGNEE_DELETE_MODE");
  if (env === "hard") return "hard";
  if (env === "soft") return "soft";
  return raw.deleteMode === "hard" ? "hard" : DEFAULT_DELETE_MODE;
}

function pickSearchType(raw: CogneePluginConfig): CogneeSearchType {
  const env = envTrim("COGNEE_SEARCH_TYPE");
  if (env) return env as CogneeSearchType;
  return raw.searchType || DEFAULT_SEARCH_TYPE;
}

function pickRecallInjectionPosition(
  raw: CogneePluginConfig,
): (typeof VALID_RECALL_POSITIONS)[number] {
  const env = envTrim("COGNEE_RECALL_INJECTION_POSITION");
  if (env && VALID_RECALL_POSITIONS.includes(env as (typeof VALID_RECALL_POSITIONS)[number])) {
    return env as (typeof VALID_RECALL_POSITIONS)[number];
  }
  if (
    raw.recallInjectionPosition &&
    VALID_RECALL_POSITIONS.includes(raw.recallInjectionPosition)
  ) {
    return raw.recallInjectionPosition;
  }
  return DEFAULT_RECALL_INJECTION_POSITION;
}

function pickWriteScope(raw: CogneePluginConfig): MemoryScope {
  const env = envTrim("COGNEE_DEFAULT_WRITE_SCOPE");
  if (env && VALID_WRITE_SCOPES.includes(env as MemoryScope)) {
    return env as MemoryScope;
  }
  return raw.defaultWriteScope || DEFAULT_WRITE_SCOPE;
}

function pickRecallScopes(raw: CogneePluginConfig): MemoryScope[] {
  const env = envTrim("COGNEE_RECALL_SCOPES");
  if (env) {
    try {
      const parsed = JSON.parse(env);
      if (Array.isArray(parsed)) return parsed as MemoryScope[];
    } catch {
      // fall through to file/default
    }
  }
  return Array.isArray(raw.recallScopes) ? raw.recallScopes : DEFAULT_RECALL_SCOPES;
}

function pickScopeRouting(raw: CogneePluginConfig): ScopeRoute[] {
  const env = envTrim("COGNEE_SCOPE_ROUTING");
  if (env) {
    try {
      const parsed = JSON.parse(env);
      if (Array.isArray(parsed)) return parsed as ScopeRoute[];
    } catch {
      // fall through to file/default
    }
  }
  return Array.isArray(raw.scopeRouting) ? raw.scopeRouting : DEFAULT_SCOPE_ROUTING;
}

function resolveApiKey(raw: CogneePluginConfig): string {
  const env = envTrim("COGNEE_API_KEY");
  if (env !== undefined) return env;
  if (raw.apiKey && raw.apiKey.length > 0) {
    return resolveEnvVars(raw.apiKey);
  }
  return "";
}

// ---------------------------------------------------------------------------
// Config resolution (precedence: env > plugin config file > defaults)
// ---------------------------------------------------------------------------

export function resolveConfig(rawConfig: unknown): Required<CogneePluginConfig> {
  const raw =
    rawConfig && typeof rawConfig === "object" && !Array.isArray(rawConfig)
      ? (rawConfig as CogneePluginConfig)
      : {};

  const mode = pickMode(raw);
  const baseUrl = pickString({
    envKeys: "COGNEE_BASE_URL",
    file: raw.baseUrl,
    defaultValue: DEFAULT_BASE_URL,
  });
  const datasetName = pickString({
    envKeys: ["COGNEE_PLUGIN_DATASET", "COGNEE_DATASET"],
    file: raw.datasetName,
    defaultValue: DEFAULT_DATASET_NAME,
  });
  const searchType = pickSearchType(raw);
  const searchPrompt = pickOptionalString({ envKeys: "COGNEE_SEARCH_PROMPT", file: raw.searchPrompt });
  const deleteMode = pickDeleteMode(raw);
  const maxResults = pickNumber({
    envKey: "COGNEE_MAX_RESULTS",
    file: raw.maxResults,
    defaultValue: DEFAULT_MAX_RESULTS,
  });
  const minScore = pickNumber({
    envKey: "COGNEE_MIN_SCORE",
    file: raw.minScore,
    defaultValue: DEFAULT_MIN_SCORE,
  });
  const maxTokens = pickNumber({
    envKey: "COGNEE_MAX_TOKENS",
    file: raw.maxTokens,
    defaultValue: DEFAULT_MAX_TOKENS,
  });
  const autoRecall = pickBool({
    envKey: "COGNEE_AUTO_RECALL",
    file: raw.autoRecall,
    defaultValue: DEFAULT_AUTO_RECALL,
  });
  const autoIndex = pickBool({
    envKey: "COGNEE_AUTO_INDEX",
    file: raw.autoIndex,
    defaultValue: DEFAULT_AUTO_INDEX,
  });
  const autoCognify = pickBool({
    envKey: "COGNEE_AUTO_COGNIFY",
    file: raw.autoCognify,
    defaultValue: DEFAULT_AUTO_COGNIFY,
  });
  const autoMemify = pickBool({
    envKey: "COGNEE_AUTO_MEMIFY",
    file: raw.autoMemify,
    defaultValue: DEFAULT_AUTO_MEMIFY,
  });
  const improveOnSessionEnd = pickBool({
    envKey: "COGNEE_IMPROVE_ON_SESSION_END",
    file: raw.improveOnSessionEnd,
    defaultValue: DEFAULT_IMPROVE_ON_SESSION_END,
  });
  const requestTimeoutMs = pickNumber({
    envKey: "COGNEE_REQUEST_TIMEOUT_MS",
    file: raw.requestTimeoutMs,
    defaultValue: DEFAULT_REQUEST_TIMEOUT_MS,
  });
  const ingestionTimeoutMs = pickNumber({
    envKey: "COGNEE_INGESTION_TIMEOUT_MS",
    file: raw.ingestionTimeoutMs,
    defaultValue: DEFAULT_INGESTION_TIMEOUT_MS,
  });

  const apiKey = resolveApiKey(raw);
  const username = pickOptionalString({ envKeys: "COGNEE_USERNAME", file: raw.username });
  const password = pickOptionalString({ envKeys: "COGNEE_PASSWORD", file: raw.password });

  const companyDataset = pickOptionalString({
    envKeys: "COGNEE_COMPANY_DATASET",
    file: raw.companyDataset,
  });
  const userDatasetPrefix = pickOptionalString({
    envKeys: "COGNEE_USER_DATASET_PREFIX",
    file: raw.userDatasetPrefix,
  });
  const agentDatasetPrefix = pickOptionalString({
    envKeys: "COGNEE_AGENT_DATASET_PREFIX",
    file: raw.agentDatasetPrefix,
  });
  const agentDatasetTemplate = pickOptionalString({
    envKeys: "COGNEE_AGENT_DATASET_TEMPLATE",
    file: raw.agentDatasetTemplate,
  });
  const userId = pickOptionalString({ envKeys: "OPENCLAW_USER_ID", file: raw.userId });
  const agentId = pickString({
    envKeys: "OPENCLAW_AGENT_ID",
    file: raw.agentId,
    defaultValue: "default",
  });
  const recallScopes = pickRecallScopes(raw);
  const defaultWriteScope = pickWriteScope(raw);
  const scopeRouting = pickScopeRouting(raw);

  const perAgentMemory = pickBool({
    envKey: "COGNEE_PER_AGENT_MEMORY",
    file: raw.perAgentMemory,
    defaultValue: false,
  });

  const recallInjectionPosition = pickRecallInjectionPosition(raw);

  const enableSessions = pickBool({
    envKey: "COGNEE_ENABLE_SESSIONS",
    file: raw.enableSessions,
    defaultValue: true,
  });
  const persistSessionsAfterEnd = pickBool({
    envKey: "COGNEE_PERSIST_SESSIONS_AFTER_END",
    file: raw.persistSessionsAfterEnd,
    defaultValue: true,
  });

  return {
    mode,
    baseUrl,
    apiKey,
    username,
    password,
    datasetName,
    companyDataset,
    userDatasetPrefix,
    agentDatasetPrefix,
    agentDatasetTemplate,
    userId,
    agentId,
    recallScopes,
    defaultWriteScope,
    scopeRouting,
    perAgentMemory,
    recallInjectionPosition,
    enableSessions,
    persistSessionsAfterEnd,
    searchType,
    searchPrompt,
    deleteMode,
    maxResults,
    minScore,
    maxTokens,
    autoRecall,
    autoIndex,
    autoCognify,
    autoMemify,
    improveOnSessionEnd,
    requestTimeoutMs,
    ingestionTimeoutMs,
  };
}
