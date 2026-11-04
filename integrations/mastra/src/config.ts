/**
 * `resolveConfig()` — the single place in this package that reads
 * `process.env`. Resolution order: explicit argument -> environment
 * variable -> default, field by field. `client.ts`/`processors.ts`/`tools.ts`
 * call it once and program against the fully-defaulted `ResolvedCogneeConfig`.
 * `env` defaults to `process.env` but is injectable for tests.
 */

import type {
  CogneeAuthConfig,
  CogneeMastraConfig,
  CogneeRecallConfig,
  CogneeToolsConfig,
  CogneeWriteConfig,
  SearchType,
} from "./types.js";

// ---- Defaults ----

export const DEFAULT_BASE_URL = "http://localhost:8000";
export const DEFAULT_DATASET = "mastra";
export const DEFAULT_DATASET_PREFIX = "mastra_";
export const DEFAULT_SCOPE = "tagged" as const;

export const DEFAULT_RECALL_ENABLED = true;
/** CHUNKS, not GRAPH_COMPLETION, for the automatic per-turn injection path. */
export const DEFAULT_SEARCH_TYPE: SearchType = "CHUNKS";
export const DEFAULT_TOP_K = 10;
export const DEFAULT_MIN_QUERY_LENGTH = 8;
export const DEFAULT_RECALL_TIMEOUT_MS = 2_500;
export const DEFAULT_RECALL_BUDGET_MS = 4_000;
export const DEFAULT_INCLUDE_REFERENCES = true;

export const DEFAULT_WRITE_MODE = "always" as const;
export const DEFAULT_RUN_IN_BACKGROUND = true;
export const DEFAULT_WRITE_MAX_CHARS = 8_000;

export const DEFAULT_ENABLE_FORGET = false;
/**
 * `tools.ts`'s per-call budget for cognee_search/cognee_ask/cognee_remember/cognee_forget — well
 * below `requestTimeoutMs`'s 30s default so a model-facing tool call can't inherit 30s x
 * (retries+1) worst-case latency.
 */
export const DEFAULT_TOOLS_TIMEOUT_MS = 10_000;

export const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
export const DEFAULT_RETRIES = 3;
export const DEFAULT_DEBUG = false;

// ---- Resolved shape ----

export type ResolvedCogneeRecallConfig = Required<CogneeRecallConfig>;
export type ResolvedCogneeWriteConfig = Required<CogneeWriteConfig>;
export type ResolvedCogneeToolsConfig = Required<CogneeToolsConfig>;

/**
 * `CogneeMastraConfig` with every field that has a default filled in.
 * `apiKey`, `auth`, `nodeSet`, `fetch` stay optional — they have no
 * meaningful default (empty `nodeSet` is the identity value; `fetch` is only ever explicit).
 */
export interface ResolvedCogneeConfig {
  baseUrl: string;
  apiKey?: string;
  auth?: CogneeAuthConfig;

  dataset: string;
  datasetPrefix: string;
  scope: "tagged" | "dataset-per-resource";
  nodeSet: string[];

  recall: ResolvedCogneeRecallConfig;
  write: ResolvedCogneeWriteConfig;
  tools: ResolvedCogneeToolsConfig;

  requestTimeoutMs: number;
  retries: number;
  debug: boolean;
  fetch?: typeof fetch;
}

// ---- Env-parsing helpers ----
// Tolerant of malformed values: a typo'd env var falls back to the default rather than propagating
// NaN/undefined.

function readString(env: NodeJS.ProcessEnv, key: string): string | undefined {
  const value = env[key];
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}

function readInt(env: NodeJS.ProcessEnv, key: string): number | undefined {
  const raw = readString(env, key);
  if (raw === undefined) return undefined;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function readBool(env: NodeJS.ProcessEnv, key: string): boolean | undefined {
  const raw = readString(env, key)?.toLowerCase();
  if (raw === undefined) return undefined;
  if (["1", "true", "yes", "on"].includes(raw)) return true;
  if (["0", "false", "no", "off"].includes(raw)) return false;
  return undefined;
}

function resolveField<T>(explicit: T | undefined, envValue: T | undefined, fallback: T): T {
  if (explicit !== undefined) return explicit;
  if (envValue !== undefined) return envValue;
  return fallback;
}

// ---- resolveConfig ----

/**
 * Resolve a `CogneeMastraConfig` partial into a fully-defaulted
 * `ResolvedCogneeConfig`. Never throws for a missing credential — safe to
 * call at module scope before any credential exists; `CogneeClient` raises on the first request
 * that actually needs one.
 */
export function resolveConfig(
  partial: CogneeMastraConfig = {},
  env: NodeJS.ProcessEnv = process.env,
): ResolvedCogneeConfig {
  const baseUrl = resolveField(partial.baseUrl, readString(env, "COGNEE_API_URL"), DEFAULT_BASE_URL);

  // apiKey wins over auth when both are present (enforced in client.ts); both are resolved
  // independently so neither is silently dropped from the resolved config.
  const apiKey = resolveField(partial.apiKey, readString(env, "COGNEE_API_KEY"), undefined);

  const email = resolveField(partial.auth?.email, readString(env, "COGNEE_USER_EMAIL"), undefined);
  const password = resolveField(partial.auth?.password, readString(env, "COGNEE_USER_PASSWORD"), undefined);
  const auth: CogneeAuthConfig | undefined =
    email !== undefined || password !== undefined ? { email, password } : undefined;

  const dataset = resolveField(partial.dataset, readString(env, "COGNEE_DATASET"), DEFAULT_DATASET);
  const datasetPrefix = resolveField(
    partial.datasetPrefix,
    readString(env, "COGNEE_DATASET_PREFIX"),
    DEFAULT_DATASET_PREFIX,
  );

  const rawScope = resolveField(partial.scope, readString(env, "COGNEE_SCOPE"), DEFAULT_SCOPE);
  const scope: "tagged" | "dataset-per-resource" =
    rawScope === "tagged" || rawScope === "dataset-per-resource" ? rawScope : DEFAULT_SCOPE;

  const nodeSet = partial.nodeSet ? [...partial.nodeSet] : [];

  const recall: ResolvedCogneeRecallConfig = {
    enabled: resolveField(
      partial.recall?.enabled,
      readBool(env, "COGNEE_RECALL_ENABLED"),
      DEFAULT_RECALL_ENABLED,
    ),
    // Passed through as-is (not validated against a runtime enum) so this package never trails the
    // server's own SearchType growth.
    searchType: resolveField(
      partial.recall?.searchType,
      readString(env, "COGNEE_SEARCH_TYPE") as SearchType | undefined,
      DEFAULT_SEARCH_TYPE,
    ),
    topK: resolveField(partial.recall?.topK, readInt(env, "COGNEE_TOP_K"), DEFAULT_TOP_K),
    minQueryLength: resolveField(partial.recall?.minQueryLength, undefined, DEFAULT_MIN_QUERY_LENGTH),
    timeoutMs: resolveField(
      partial.recall?.timeoutMs,
      readInt(env, "COGNEE_RECALL_TIMEOUT_MS"),
      DEFAULT_RECALL_TIMEOUT_MS,
    ),
    budgetMs: resolveField(
      partial.recall?.budgetMs,
      readInt(env, "COGNEE_RECALL_BUDGET_MS"),
      DEFAULT_RECALL_BUDGET_MS,
    ),
    includeReferences: resolveField(partial.recall?.includeReferences, undefined, DEFAULT_INCLUDE_REFERENCES),
  };

  const rawWriteMode = resolveField(partial.write?.mode, readString(env, "COGNEE_SAVE_MODE"), DEFAULT_WRITE_MODE);
  const writeMode: "always" | "assistant-only" | "never" =
    rawWriteMode === "always" || rawWriteMode === "assistant-only" || rawWriteMode === "never"
      ? rawWriteMode
      : DEFAULT_WRITE_MODE;

  const write: ResolvedCogneeWriteConfig = {
    mode: writeMode,
    runInBackground: resolveField(partial.write?.runInBackground, undefined, DEFAULT_RUN_IN_BACKGROUND),
    maxChars: resolveField(partial.write?.maxChars, undefined, DEFAULT_WRITE_MAX_CHARS),
  };

  const tools: ResolvedCogneeToolsConfig = {
    enableForget: resolveField(
      partial.tools?.enableForget,
      readBool(env, "COGNEE_ENABLE_FORGET"),
      DEFAULT_ENABLE_FORGET,
    ),
    timeoutMs: resolveField(
      partial.tools?.timeoutMs,
      readInt(env, "COGNEE_TOOLS_TIMEOUT_MS"),
      DEFAULT_TOOLS_TIMEOUT_MS,
    ),
  };

  const requestTimeoutMs = resolveField(
    partial.requestTimeoutMs,
    readInt(env, "COGNEE_TIMEOUT_MS"),
    DEFAULT_REQUEST_TIMEOUT_MS,
  );
  const retries = resolveField(partial.retries, readInt(env, "COGNEE_RETRIES"), DEFAULT_RETRIES);
  const debug = resolveField(partial.debug, readBool(env, "COGNEE_DEBUG"), DEFAULT_DEBUG);

  const resolved: ResolvedCogneeConfig = {
    baseUrl,
    apiKey,
    auth,
    dataset,
    datasetPrefix,
    scope,
    nodeSet,
    recall,
    write,
    tools,
    requestTimeoutMs,
    retries,
    debug,
    fetch: partial.fetch,
  };

  if (debug) {
    // eslint-disable-next-line no-console -- intentional: this *is* the debug channel.
    console.debug("[cognee-mastra] resolved config:", redactConfigForLogging(resolved));
  }

  return resolved;
}

// ---- Redaction: the only thing this package is allowed to hand to a logger ----

const REDACTED = "***REDACTED***";

/**
 * A copy of a resolved config safe to log: `apiKey`/`auth.password` masked,
 * `fetch` dropped entirely (a function value logs as noise and carries no
 * secret). `client.ts`'s own debug logging should route through this same helper rather than
 * re-deriving redaction rules.
 */
export function redactConfigForLogging(config: ResolvedCogneeConfig): Record<string, unknown> {
  const { fetch: _fetch, ...rest } = config;
  return {
    ...rest,
    apiKey: rest.apiKey !== undefined ? REDACTED : undefined,
    auth:
      rest.auth !== undefined
        ? { email: rest.auth.email, password: rest.auth.password !== undefined ? REDACTED : undefined }
        : undefined,
  };
}
