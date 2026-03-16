import type { CogneePluginConfig, CogneeSearchType, MemoryScope, ScopeRoute } from "./types.js";

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

export const DEFAULT_BASE_URL = "http://localhost:8000";
export const DEFAULT_DATASET_NAME = "openclaw";
export const DEFAULT_SEARCH_TYPE: CogneeSearchType = "FEELING_LUCKY";
export const DEFAULT_DELETE_MODE = "soft" as const;
export const DEFAULT_MAX_RESULTS = 6;
export const DEFAULT_MIN_SCORE = 0;
export const DEFAULT_MAX_TOKENS = 512;
export const DEFAULT_AUTO_RECALL = true;
export const DEFAULT_AUTO_INDEX = true;
export const DEFAULT_AUTO_COGNIFY = true;
export const DEFAULT_AUTO_MEMIFY = false;
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

// ---------------------------------------------------------------------------
// Config resolution
// ---------------------------------------------------------------------------

export function resolveConfig(rawConfig: unknown): Required<CogneePluginConfig> {
  const raw =
    rawConfig && typeof rawConfig === "object" && !Array.isArray(rawConfig)
      ? (rawConfig as CogneePluginConfig)
      : {};

  const baseUrl = raw.baseUrl?.trim() || DEFAULT_BASE_URL;
  const datasetName = raw.datasetName?.trim() || DEFAULT_DATASET_NAME;
  const searchType = raw.searchType || DEFAULT_SEARCH_TYPE;
  const searchPrompt = raw.searchPrompt || "";
  const deleteMode = raw.deleteMode === "hard" ? "hard" : DEFAULT_DELETE_MODE;
  const maxResults = typeof raw.maxResults === "number" ? raw.maxResults : DEFAULT_MAX_RESULTS;
  const minScore = typeof raw.minScore === "number" ? raw.minScore : DEFAULT_MIN_SCORE;
  const maxTokens = typeof raw.maxTokens === "number" ? raw.maxTokens : DEFAULT_MAX_TOKENS;
  const autoRecall = typeof raw.autoRecall === "boolean" ? raw.autoRecall : DEFAULT_AUTO_RECALL;
  const autoIndex = typeof raw.autoIndex === "boolean" ? raw.autoIndex : DEFAULT_AUTO_INDEX;
  const autoCognify = typeof raw.autoCognify === "boolean" ? raw.autoCognify : DEFAULT_AUTO_COGNIFY;
  const autoMemify = typeof raw.autoMemify === "boolean" ? raw.autoMemify : DEFAULT_AUTO_MEMIFY;
  const requestTimeoutMs = typeof raw.requestTimeoutMs === "number" ? raw.requestTimeoutMs : DEFAULT_REQUEST_TIMEOUT_MS;
  const ingestionTimeoutMs = typeof raw.ingestionTimeoutMs === "number" ? raw.ingestionTimeoutMs : DEFAULT_INGESTION_TIMEOUT_MS;

  const apiKey =
    raw.apiKey && raw.apiKey.length > 0 ? resolveEnvVars(raw.apiKey) : process.env.COGNEE_API_KEY || "";
  const username = raw.username?.trim() || process.env.COGNEE_USERNAME || "";
  const password = raw.password?.trim() || process.env.COGNEE_PASSWORD || "";

  // Multi-scope
  const companyDataset = raw.companyDataset?.trim() || "";
  const userDatasetPrefix = raw.userDatasetPrefix?.trim() || "";
  const agentDatasetPrefix = raw.agentDatasetPrefix?.trim() || "";
  const userId = raw.userId?.trim() || process.env.OPENCLAW_USER_ID || "";
  const agentId = raw.agentId?.trim() || process.env.OPENCLAW_AGENT_ID || "default";
  const recallScopes = Array.isArray(raw.recallScopes) ? raw.recallScopes : DEFAULT_RECALL_SCOPES;
  const defaultWriteScope = raw.defaultWriteScope || DEFAULT_WRITE_SCOPE;
  const scopeRouting = Array.isArray(raw.scopeRouting) ? raw.scopeRouting : DEFAULT_SCOPE_ROUTING;

  // Session
  const enableSessions = typeof raw.enableSessions === "boolean" ? raw.enableSessions : true;
  const persistSessionsAfterEnd = typeof raw.persistSessionsAfterEnd === "boolean" ? raw.persistSessionsAfterEnd : true;

  return {
    baseUrl, apiKey, username, password, datasetName,
    companyDataset, userDatasetPrefix, agentDatasetPrefix, userId, agentId,
    recallScopes, defaultWriteScope, scopeRouting,
    enableSessions, persistSessionsAfterEnd,
    searchType, searchPrompt, deleteMode,
    maxResults, minScore, maxTokens,
    autoRecall, autoIndex, autoCognify, autoMemify,
    requestTimeoutMs, ingestionTimeoutMs,
  };
}
