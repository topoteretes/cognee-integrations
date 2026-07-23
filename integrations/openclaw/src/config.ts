import type { CogneeMode, CogneePluginConfig, CogneeSearchType, MemoryScope, ScopeRoute } from "./types.js";
import { sanitizeDatasetName } from "./scope.js";

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

export const DEFAULT_BASE_URL = "http://localhost:8011";
export const DEFAULT_DATASET_NAME = "agent_sessions";
export const DEFAULT_SEARCH_TYPE: CogneeSearchType = "HYBRID_COMPLETION";
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

// Recall hot path — same defaults as the claude-code/codex integrations
// (COGNEE_RECALL_TIMEOUT=2.5s, COGNEE_RECALL_BUDGET=4s,
//  COGNEE_BREAKER_THRESHOLD=5, COGNEE_BREAKER_COOLDOWN=120s).
export const DEFAULT_RECALL_TIMEOUT_MS = 2_500;
export const DEFAULT_RECALL_BUDGET_MS = 4_000;
export const DEFAULT_RECALL_BREAKER_THRESHOLD = 5;
export const DEFAULT_RECALL_BREAKER_COOLDOWN_MS = 120_000;

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

  const mode: CogneeMode = raw.mode === "cloud" || process.env.COGNEE_MODE === "cloud" ? "cloud" : "local";
  const baseUrl = raw.baseUrl?.trim() || process.env.COGNEE_BASE_URL?.trim() || DEFAULT_BASE_URL;
  const datasetName = sanitizeDatasetName(
    process.env.COGNEE_PLUGIN_DATASET?.trim() || raw.datasetName?.trim() || DEFAULT_DATASET_NAME,
  );
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
  const improveOnSessionEnd = typeof raw.improveOnSessionEnd === "boolean" ? raw.improveOnSessionEnd : DEFAULT_IMPROVE_ON_SESSION_END;
  const requestTimeoutMs = typeof raw.requestTimeoutMs === "number" ? raw.requestTimeoutMs : DEFAULT_REQUEST_TIMEOUT_MS;
  const ingestionTimeoutMs = typeof raw.ingestionTimeoutMs === "number" ? raw.ingestionTimeoutMs : DEFAULT_INGESTION_TIMEOUT_MS;
  const recallTimeoutMs = typeof raw.recallTimeoutMs === "number" ? raw.recallTimeoutMs : DEFAULT_RECALL_TIMEOUT_MS;
  const recallBudgetMs = typeof raw.recallBudgetMs === "number" ? raw.recallBudgetMs : DEFAULT_RECALL_BUDGET_MS;
  const recallBreakerThreshold = typeof raw.recallBreakerThreshold === "number" ? raw.recallBreakerThreshold : DEFAULT_RECALL_BREAKER_THRESHOLD;
  const recallBreakerCooldownMs = typeof raw.recallBreakerCooldownMs === "number" ? raw.recallBreakerCooldownMs : DEFAULT_RECALL_BREAKER_COOLDOWN_MS;

  const apiKey =
    raw.apiKey && raw.apiKey.length > 0 ? resolveEnvVars(raw.apiKey)
    : mode === "cloud" ? process.env.COGNEE_API_KEY || ""
    : "";
  const username = raw.username?.trim() || process.env.COGNEE_USERNAME || "";
  const password = raw.password?.trim() || process.env.COGNEE_PASSWORD || "";

  // Multi-scope
  const companyDataset = raw.companyDataset?.trim() || "";
  const userDatasetPrefix = raw.userDatasetPrefix?.trim() || "";
  const agentDatasetPrefix = raw.agentDatasetPrefix?.trim() || "";
  const agentDatasetTemplate = raw.agentDatasetTemplate?.trim() || "";
  const userId = raw.userId?.trim() || process.env.OPENCLAW_USER_ID || "";
  const agentId = raw.agentId?.trim() || process.env.OPENCLAW_AGENT_ID || "default";
  const recallScopes = Array.isArray(raw.recallScopes) ? raw.recallScopes : DEFAULT_RECALL_SCOPES;
  const defaultWriteScope = raw.defaultWriteScope || DEFAULT_WRITE_SCOPE;
  const scopeRouting = Array.isArray(raw.scopeRouting) ? raw.scopeRouting : DEFAULT_SCOPE_ROUTING;

  // Per-agent memory: strictly opt-in, never auto-enabled. All agents share
  // one dataset by default (matching the claude-code/codex integrations).
  // Note that isolation also requires multi-scope to be active: plugin.ts
  // gates on `multiScope && cfg.perAgentMemory`, so users must set an
  // agentDatasetPrefix/agentDatasetTemplate alongside this flag.
  const perAgentMemory = typeof raw.perAgentMemory === "boolean" ? raw.perAgentMemory : false;

  // Recall injection
  const validPositions = ["prependSystemContext", "appendSystemContext", "prependContext"] as const;
  const recallInjectionPosition = raw.recallInjectionPosition && validPositions.includes(raw.recallInjectionPosition)
    ? raw.recallInjectionPosition
    : DEFAULT_RECALL_INJECTION_POSITION;

  // Session
  const enableSessions = typeof raw.enableSessions === "boolean" ? raw.enableSessions : true;
  const persistSessionsAfterEnd = typeof raw.persistSessionsAfterEnd === "boolean" ? raw.persistSessionsAfterEnd : true;
  const captureSession = typeof raw.captureSession === "boolean" ? raw.captureSession : true;

  return {
    mode, baseUrl, apiKey, username, password, datasetName,
    companyDataset, userDatasetPrefix, agentDatasetPrefix, agentDatasetTemplate, userId, agentId,
    recallScopes, defaultWriteScope, scopeRouting, perAgentMemory,
    recallInjectionPosition,
    enableSessions, persistSessionsAfterEnd, captureSession,
    searchType, searchPrompt, deleteMode,
    maxResults, minScore, maxTokens,
    autoRecall, autoIndex, autoCognify, autoMemify, improveOnSessionEnd,
    requestTimeoutMs, ingestionTimeoutMs,
    recallTimeoutMs, recallBudgetMs, recallBreakerThreshold, recallBreakerCooldownMs,
  };
}
