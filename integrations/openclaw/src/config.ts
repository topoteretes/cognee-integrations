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

type EnvResolver<T> = (value: string) => T | undefined;

function envValue<T>(name: string, resolve: EnvResolver<T>): T | undefined {
  const value = process.env[name];
  if (value === undefined || value.trim() === "") {
    return undefined;
  }
  return resolve(value);
}

const envString = (name: string): string | undefined => envValue(name, (value) => value.trim());
const envNumber = (name: string): number | undefined =>
  envValue(name, (value) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  });
const envBoolean = (name: string): boolean | undefined =>
  envValue(name, (value) => {
    const normalized = value.trim().toLowerCase();
    if (["1", "true", "yes", "y", "on"].includes(normalized)) return true;
    if (["0", "false", "no", "n", "off"].includes(normalized)) return false;
    return undefined;
  });
const envJson = <T>(name: string): T | undefined =>
  envValue(name, (value) => {
    try {
      return JSON.parse(value) as T;
    } catch {
      return undefined;
    }
  });

function envEnum<T extends string>(name: string, allowed: readonly T[]): T | undefined {
  return envValue(name, (value) => {
    const normalized = value.trim();
    return allowed.includes(normalized as T) ? (normalized as T) : undefined;
  });
}

function envScopes(name: string): MemoryScope[] | undefined {
  return envValue(name, (value) => {
    const parsed = value.trim().startsWith("[")
      ? envJson<unknown[]>(name)
      : value.split(",").map((item) => item.trim());
    if (!Array.isArray(parsed) || parsed.length === 0) return undefined;
    return parsed.every((item): item is MemoryScope =>
      item === "agent" || item === "user" || item === "company"
    )
      ? parsed
      : undefined;
  });
}

function envConfig(): CogneePluginConfig {
  const cfg: CogneePluginConfig = {};
  const mode = envEnum<CogneeMode>("COGNEE_MODE", ["local", "cloud"]);
  if (mode) cfg.mode = mode;
  cfg.baseUrl = envString("COGNEE_BASE_URL");
  cfg.apiKey = envString("COGNEE_API_KEY");
  cfg.username = envString("COGNEE_USERNAME");
  cfg.password = envString("COGNEE_PASSWORD");
  cfg.datasetName = envString("COGNEE_OPENCLAW_DATASET_NAME");
  cfg.companyDataset = envString("COGNEE_OPENCLAW_COMPANY_DATASET");
  cfg.userDatasetPrefix = envString("COGNEE_OPENCLAW_USER_DATASET_PREFIX");
  cfg.agentDatasetPrefix = envString("COGNEE_OPENCLAW_AGENT_DATASET_PREFIX");
  cfg.agentDatasetTemplate = envString("COGNEE_OPENCLAW_AGENT_DATASET_TEMPLATE");
  cfg.userId = envString("OPENCLAW_USER_ID") || envString("COGNEE_OPENCLAW_USER_ID");
  cfg.agentId = envString("OPENCLAW_AGENT_ID") || envString("COGNEE_OPENCLAW_AGENT_ID");
  cfg.recallScopes = envScopes("COGNEE_OPENCLAW_RECALL_SCOPES");
  cfg.defaultWriteScope = envEnum<MemoryScope>("COGNEE_OPENCLAW_DEFAULT_WRITE_SCOPE", [
    "company",
    "user",
    "agent",
  ]);
  cfg.scopeRouting = envJson<ScopeRoute[]>("COGNEE_OPENCLAW_SCOPE_ROUTING");
  cfg.perAgentMemory = envBoolean("COGNEE_OPENCLAW_PER_AGENT_MEMORY");
  cfg.recallInjectionPosition = envEnum("COGNEE_OPENCLAW_RECALL_INJECTION_POSITION", [
    "prependSystemContext",
    "appendSystemContext",
    "prependContext",
  ] as const);
  cfg.enableSessions = envBoolean("COGNEE_OPENCLAW_ENABLE_SESSIONS");
  cfg.persistSessionsAfterEnd = envBoolean("COGNEE_OPENCLAW_PERSIST_SESSIONS_AFTER_END");
  cfg.searchType = envEnum<CogneeSearchType>("COGNEE_OPENCLAW_SEARCH_TYPE", [
    "GRAPH_COMPLETION",
    "GRAPH_COMPLETION_COT",
    "GRAPH_COMPLETION_CONTEXT_EXTENSION",
    "GRAPH_SUMMARY_COMPLETION",
    "RAG_COMPLETION",
    "TRIPLET_COMPLETION",
    "CHUNKS",
    "CHUNKS_LEXICAL",
    "SUMMARIES",
    "CYPHER",
    "NATURAL_LANGUAGE",
    "TEMPORAL",
    "CODING_RULES",
    "FEELING_LUCKY",
  ]);
  cfg.searchPrompt = envString("COGNEE_OPENCLAW_SEARCH_PROMPT");
  cfg.deleteMode = envEnum("COGNEE_OPENCLAW_DELETE_MODE", ["soft", "hard"] as const);
  cfg.maxResults = envNumber("COGNEE_OPENCLAW_MAX_RESULTS");
  cfg.minScore = envNumber("COGNEE_OPENCLAW_MIN_SCORE");
  cfg.maxTokens = envNumber("COGNEE_OPENCLAW_MAX_TOKENS");
  cfg.autoRecall = envBoolean("COGNEE_OPENCLAW_AUTO_RECALL");
  cfg.autoIndex = envBoolean("COGNEE_OPENCLAW_AUTO_INDEX");
  cfg.autoCognify = envBoolean("COGNEE_OPENCLAW_AUTO_COGNIFY");
  cfg.autoMemify = envBoolean("COGNEE_OPENCLAW_AUTO_MEMIFY");
  cfg.improveOnSessionEnd = envBoolean("COGNEE_OPENCLAW_IMPROVE_ON_SESSION_END");
  cfg.requestTimeoutMs = envNumber("COGNEE_OPENCLAW_REQUEST_TIMEOUT_MS");
  cfg.ingestionTimeoutMs = envNumber("COGNEE_OPENCLAW_INGESTION_TIMEOUT_MS");
  return Object.fromEntries(
    Object.entries(cfg).filter(([, value]) => value !== undefined),
  ) as CogneePluginConfig;
}

export function resolveConfig(rawConfig: unknown): Required<CogneePluginConfig> {
  const raw =
    rawConfig && typeof rawConfig === "object" && !Array.isArray(rawConfig)
      ? (rawConfig as CogneePluginConfig)
      : {};
  const env = envConfig();

  const mode: CogneeMode = env.mode || raw.mode || "local";
  const baseUrl = env.baseUrl?.trim() || raw.baseUrl?.trim() || DEFAULT_BASE_URL;
  const datasetName = env.datasetName?.trim() || raw.datasetName?.trim() || DEFAULT_DATASET_NAME;
  const searchType = env.searchType || raw.searchType || DEFAULT_SEARCH_TYPE;
  const searchPrompt = env.searchPrompt || raw.searchPrompt || "";
  const deleteMode = env.deleteMode || (raw.deleteMode === "hard" ? "hard" : DEFAULT_DELETE_MODE);
  const maxResults = typeof env.maxResults === "number" ? env.maxResults : typeof raw.maxResults === "number" ? raw.maxResults : DEFAULT_MAX_RESULTS;
  const minScore = typeof env.minScore === "number" ? env.minScore : typeof raw.minScore === "number" ? raw.minScore : DEFAULT_MIN_SCORE;
  const maxTokens = typeof env.maxTokens === "number" ? env.maxTokens : typeof raw.maxTokens === "number" ? raw.maxTokens : DEFAULT_MAX_TOKENS;
  const autoRecall = typeof env.autoRecall === "boolean" ? env.autoRecall : typeof raw.autoRecall === "boolean" ? raw.autoRecall : DEFAULT_AUTO_RECALL;
  const autoIndex = typeof env.autoIndex === "boolean" ? env.autoIndex : typeof raw.autoIndex === "boolean" ? raw.autoIndex : DEFAULT_AUTO_INDEX;
  const autoCognify = typeof env.autoCognify === "boolean" ? env.autoCognify : typeof raw.autoCognify === "boolean" ? raw.autoCognify : DEFAULT_AUTO_COGNIFY;
  const autoMemify = typeof env.autoMemify === "boolean" ? env.autoMemify : typeof raw.autoMemify === "boolean" ? raw.autoMemify : DEFAULT_AUTO_MEMIFY;
  const improveOnSessionEnd = typeof env.improveOnSessionEnd === "boolean" ? env.improveOnSessionEnd : typeof raw.improveOnSessionEnd === "boolean" ? raw.improveOnSessionEnd : DEFAULT_IMPROVE_ON_SESSION_END;
  const requestTimeoutMs = typeof env.requestTimeoutMs === "number" ? env.requestTimeoutMs : typeof raw.requestTimeoutMs === "number" ? raw.requestTimeoutMs : DEFAULT_REQUEST_TIMEOUT_MS;
  const ingestionTimeoutMs = typeof env.ingestionTimeoutMs === "number" ? env.ingestionTimeoutMs : typeof raw.ingestionTimeoutMs === "number" ? raw.ingestionTimeoutMs : DEFAULT_INGESTION_TIMEOUT_MS;

  const apiKey =
    env.apiKey && env.apiKey.length > 0 ? resolveEnvVars(env.apiKey)
    : raw.apiKey && raw.apiKey.length > 0 ? resolveEnvVars(raw.apiKey)
    : mode === "cloud" ? process.env.COGNEE_API_KEY || ""
    : "";
  const username = env.username?.trim() || raw.username?.trim() || "";
  const password = env.password?.trim() || raw.password?.trim() || "";

  // Multi-scope
  const companyDataset = env.companyDataset?.trim() || raw.companyDataset?.trim() || "";
  const userDatasetPrefix = env.userDatasetPrefix?.trim() || raw.userDatasetPrefix?.trim() || "";
  const agentDatasetPrefix = env.agentDatasetPrefix?.trim() || raw.agentDatasetPrefix?.trim() || "";
  const agentDatasetTemplate = env.agentDatasetTemplate?.trim() || raw.agentDatasetTemplate?.trim() || "";
  const userId = env.userId?.trim() || raw.userId?.trim() || "";
  const agentId = env.agentId?.trim() || raw.agentId?.trim() || "default";
  const recallScopes = Array.isArray(env.recallScopes) ? env.recallScopes : Array.isArray(raw.recallScopes) ? raw.recallScopes : DEFAULT_RECALL_SCOPES;
  const defaultWriteScope = env.defaultWriteScope || raw.defaultWriteScope || DEFAULT_WRITE_SCOPE;
  const scopeRouting = Array.isArray(env.scopeRouting) ? env.scopeRouting : Array.isArray(raw.scopeRouting) ? raw.scopeRouting : DEFAULT_SCOPE_ROUTING;

  // Per-agent memory: opt-in. Explicit config wins. When unset, it defaults to
  // false here and is auto-enabled in plugin.ts only when the gateway hosts
  // multiple agents (agents.list.length > 1) — so single-agent installs keep
  // the legacy shared behavior and are unaffected by the upgrade.
  const perAgentMemory = typeof env.perAgentMemory === "boolean" ? env.perAgentMemory : typeof raw.perAgentMemory === "boolean" ? raw.perAgentMemory : false;

  // Recall injection
  const validPositions = ["prependSystemContext", "appendSystemContext", "prependContext"] as const;
  const recallInjectionPosition = env.recallInjectionPosition && validPositions.includes(env.recallInjectionPosition)
    ? env.recallInjectionPosition
    : raw.recallInjectionPosition && validPositions.includes(raw.recallInjectionPosition)
    ? raw.recallInjectionPosition
    : DEFAULT_RECALL_INJECTION_POSITION;

  // Session
  const enableSessions = typeof env.enableSessions === "boolean" ? env.enableSessions : typeof raw.enableSessions === "boolean" ? raw.enableSessions : true;
  const persistSessionsAfterEnd = typeof env.persistSessionsAfterEnd === "boolean" ? env.persistSessionsAfterEnd : typeof raw.persistSessionsAfterEnd === "boolean" ? raw.persistSessionsAfterEnd : true;

  return {
    mode, baseUrl, apiKey, username, password, datasetName,
    companyDataset, userDatasetPrefix, agentDatasetPrefix, agentDatasetTemplate, userId, agentId,
    recallScopes, defaultWriteScope, scopeRouting, perAgentMemory,
    recallInjectionPosition,
    enableSessions, persistSessionsAfterEnd,
    searchType, searchPrompt, deleteMode,
    maxResults, minScore, maxTokens,
    autoRecall, autoIndex, autoCognify, autoMemify, improveOnSessionEnd,
    requestTimeoutMs, ingestionTimeoutMs,
  };
}
