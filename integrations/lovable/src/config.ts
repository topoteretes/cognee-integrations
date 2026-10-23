/**
 * `resolveConfig()` is the only place that reads the environment. Precedence,
 * field by field: explicit argument → environment variable → default. The
 * resolved object is validated once; everything else programs against it.
 */
import { ConfigError } from "./errors.js";

export const DEFAULT_COGNEE_BASE_URL = "http://localhost:8000";
export const DEFAULT_HOST = "127.0.0.1";
export const DEFAULT_PORT = 8787;
export const DEFAULT_MCP_PATH = "/mcp";
export const DEFAULT_RECEIPTS_PATH = "./data/receipts.sqlite";

export const DEFAULT_REQUEST_TIMEOUT_MS = 15_000;
export const DEFAULT_WRITE_TIMEOUT_MS = 30_000;
export const DEFAULT_RECALL_TIMEOUT_MS = 20_000;
export const DEFAULT_STATUS_TIMEOUT_MS = 10_000;

export const DEFAULT_MAX_CONTENT_CHARS = 4_000;
export const DEFAULT_MAX_RATIONALE_CHARS = 2_000;
export const DEFAULT_MAX_SOURCE_REF_CHARS = 500;
export const DEFAULT_MAX_QUERY_CHARS = 500;
export const DEFAULT_RECALL_TOP_K = 5;
export const DEFAULT_MAX_RECALL_RESULTS = 10;
export const DEFAULT_MAX_RECALL_CHARS = 6_000;
export const DEFAULT_MAX_REQUEST_BODY_BYTES = 256 * 1024;
export const DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024;

export type RecallMode = "evidence" | "answer";

export interface LovableMemoryConfig {
  cognee: {
    baseUrl: string;
    apiKey: string;
    requestTimeoutMs: number;
    writeTimeoutMs: number;
    recallTimeoutMs: number;
    statusTimeoutMs: number;
    maxResponseBytes: number;
  };
  /** The one dataset this deployment writes to and always reads from. */
  projectDataset: string;
  /** Extra datasets this deployment may read; never written, never created. */
  sharedDatasets: string[];
  /** Accepted inbound bearer tokens (several, so a rotation can overlap). */
  tokens: string[];
  http: {
    host: string;
    port: number;
    path: string;
    publicUrl?: string;
    allowedHosts: string[];
    allowedOrigins: string[];
    maxRequestBodyBytes: number;
  };
  receipts: {
    path: string;
  };
  limits: {
    maxContentChars: number;
    maxRationaleChars: number;
    maxSourceRefChars: number;
    maxQueryChars: number;
    defaultTopK: number;
    maxRecallResults: number;
    maxRecallChars: number;
  };
  recall: {
    defaultMode: RecallMode;
    allowAnswerMode: boolean;
  };
  logLevel: "debug" | "info" | "warn" | "error";
}

type DeepPartial<T> = { [K in keyof T]?: T[K] extends readonly unknown[] ? T[K] : T[K] extends object ? DeepPartial<T[K]> : T[K] };
export type LovableMemoryConfigInput = DeepPartial<LovableMemoryConfig>;

type Env = Record<string, string | undefined>;

const DATASET_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

function str(env: Env, key: string): string | undefined {
  const value = env[key];
  if (value === undefined) return undefined;
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed;
}

function int(env: Env, key: string): number | undefined {
  const raw = str(env, key);
  if (raw === undefined) return undefined;
  const value = Number(raw);
  if (!Number.isFinite(value) || !Number.isInteger(value) || value < 0) {
    throw new ConfigError(`${key} must be a non-negative integer, got ${JSON.stringify(raw)}`);
  }
  return value;
}

function bool(env: Env, key: string): boolean | undefined {
  const raw = str(env, key);
  if (raw === undefined) return undefined;
  const lowered = raw.toLowerCase();
  if (["1", "true", "yes", "on"].includes(lowered)) return true;
  if (["0", "false", "no", "off"].includes(lowered)) return false;
  throw new ConfigError(`${key} must be a boolean, got ${JSON.stringify(raw)}`);
}

function list(env: Env, key: string): string[] | undefined {
  const raw = str(env, key);
  if (raw === undefined) return undefined;
  return raw
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

function stripTrailingSlash(value: string): string {
  return value.replace(/\/+$/, "");
}

export function hostFromUrl(value: string): string | undefined {
  try {
    const url = new URL(value);
    return url.host.toLowerCase();
  } catch {
    return undefined;
  }
}

export function originFromUrl(value: string): string | undefined {
  try {
    return new URL(value).origin.toLowerCase();
  } catch {
    return undefined;
  }
}

export function resolveConfig(input: LovableMemoryConfigInput = {}, env: Env = process.env): LovableMemoryConfig {
  const baseUrl = stripTrailingSlash(input.cognee?.baseUrl ?? str(env, "COGNEE_BASE_URL") ?? DEFAULT_COGNEE_BASE_URL);
  const apiKey = input.cognee?.apiKey ?? str(env, "COGNEE_API_KEY") ?? "";

  const projectDataset = input.projectDataset ?? str(env, "LOVABLE_PROJECT_DATASET") ?? "";
  const sharedDatasets = input.sharedDatasets ?? list(env, "LOVABLE_SHARED_DATASETS") ?? [];
  const tokens = input.tokens ?? list(env, "LOVABLE_MCP_TOKEN") ?? [];

  const port = input.http?.port ?? int(env, "LOVABLE_MCP_PORT") ?? int(env, "PORT") ?? DEFAULT_PORT;
  const host = input.http?.host ?? str(env, "LOVABLE_MCP_HOST") ?? DEFAULT_HOST;
  const path = input.http?.path ?? str(env, "LOVABLE_MCP_PATH") ?? DEFAULT_MCP_PATH;
  const publicUrl = input.http?.publicUrl ?? str(env, "LOVABLE_PUBLIC_URL");

  const explicitHosts = input.http?.allowedHosts ?? list(env, "LOVABLE_ALLOWED_HOSTS");
  const allowedHosts = new Set<string>((explicitHosts ?? []).map((item) => item.toLowerCase()));
  if (explicitHosts === undefined) {
    // Loopback is always a legitimate way to reach the service (health
    // checks, the operator CLI, local smoke tests); a public hostname has to be
    // declared, either directly or through LOVABLE_PUBLIC_URL.
    allowedHosts.add(`localhost:${port}`);
    allowedHosts.add(`127.0.0.1:${port}`);
    allowedHosts.add(`[::1]:${port}`);
    const publicHost = publicUrl ? hostFromUrl(publicUrl) : undefined;
    if (publicHost) allowedHosts.add(publicHost);
  }

  const explicitOrigins = input.http?.allowedOrigins ?? list(env, "LOVABLE_ALLOWED_ORIGINS");
  const allowedOrigins = new Set<string>((explicitOrigins ?? []).map((item) => item.toLowerCase()));
  if (explicitOrigins === undefined && publicUrl) {
    const origin = originFromUrl(publicUrl);
    if (origin) allowedOrigins.add(origin);
  }

  const config: LovableMemoryConfig = {
    cognee: {
      baseUrl,
      apiKey,
      requestTimeoutMs: input.cognee?.requestTimeoutMs ?? int(env, "LOVABLE_REQUEST_TIMEOUT_MS") ?? DEFAULT_REQUEST_TIMEOUT_MS,
      writeTimeoutMs: input.cognee?.writeTimeoutMs ?? int(env, "LOVABLE_WRITE_TIMEOUT_MS") ?? DEFAULT_WRITE_TIMEOUT_MS,
      recallTimeoutMs: input.cognee?.recallTimeoutMs ?? int(env, "LOVABLE_RECALL_TIMEOUT_MS") ?? DEFAULT_RECALL_TIMEOUT_MS,
      statusTimeoutMs: input.cognee?.statusTimeoutMs ?? int(env, "LOVABLE_STATUS_TIMEOUT_MS") ?? DEFAULT_STATUS_TIMEOUT_MS,
      maxResponseBytes: input.cognee?.maxResponseBytes ?? DEFAULT_MAX_RESPONSE_BYTES,
    },
    projectDataset,
    sharedDatasets: Array.from(new Set(sharedDatasets)).filter((name) => name !== projectDataset),
    tokens: Array.from(new Set(tokens)),
    http: {
      host,
      port,
      path: path.startsWith("/") ? path : `/${path}`,
      publicUrl: publicUrl ? stripTrailingSlash(publicUrl) : undefined,
      allowedHosts: Array.from(allowedHosts),
      allowedOrigins: Array.from(allowedOrigins),
      maxRequestBodyBytes: input.http?.maxRequestBodyBytes ?? int(env, "LOVABLE_MAX_REQUEST_BODY_BYTES") ?? DEFAULT_MAX_REQUEST_BODY_BYTES,
    },
    receipts: {
      path: input.receipts?.path ?? str(env, "LOVABLE_RECEIPTS_PATH") ?? DEFAULT_RECEIPTS_PATH,
    },
    limits: {
      maxContentChars: input.limits?.maxContentChars ?? int(env, "LOVABLE_MAX_CONTENT_CHARS") ?? DEFAULT_MAX_CONTENT_CHARS,
      maxRationaleChars: input.limits?.maxRationaleChars ?? int(env, "LOVABLE_MAX_RATIONALE_CHARS") ?? DEFAULT_MAX_RATIONALE_CHARS,
      maxSourceRefChars: input.limits?.maxSourceRefChars ?? DEFAULT_MAX_SOURCE_REF_CHARS,
      maxQueryChars: input.limits?.maxQueryChars ?? int(env, "LOVABLE_MAX_QUERY_CHARS") ?? DEFAULT_MAX_QUERY_CHARS,
      defaultTopK: input.limits?.defaultTopK ?? int(env, "LOVABLE_RECALL_TOP_K") ?? DEFAULT_RECALL_TOP_K,
      maxRecallResults: input.limits?.maxRecallResults ?? int(env, "LOVABLE_MAX_RECALL_RESULTS") ?? DEFAULT_MAX_RECALL_RESULTS,
      maxRecallChars: input.limits?.maxRecallChars ?? int(env, "LOVABLE_MAX_RECALL_CHARS") ?? DEFAULT_MAX_RECALL_CHARS,
    },
    recall: {
      defaultMode: input.recall?.defaultMode ?? (str(env, "LOVABLE_RECALL_MODE") as RecallMode | undefined) ?? "evidence",
      allowAnswerMode: input.recall?.allowAnswerMode ?? bool(env, "LOVABLE_RECALL_ALLOW_ANSWER_MODE") ?? true,
    },
    logLevel: (input.logLevel ?? (str(env, "LOVABLE_LOG_LEVEL") as LovableMemoryConfig["logLevel"] | undefined)) ?? "info",
  };

  validateConfig(config);
  return config;
}

export function validateConfig(config: LovableMemoryConfig): void {
  const problems: string[] = [];
  if (!/^https?:\/\//.test(config.cognee.baseUrl)) {
    problems.push(`COGNEE_BASE_URL must start with http:// or https:// (got ${JSON.stringify(config.cognee.baseUrl)})`);
  }
  if (!config.cognee.apiKey) problems.push("COGNEE_API_KEY is required: the connector authenticates to cognee with X-Api-Key");
  if (!config.projectDataset) {
    problems.push("LOVABLE_PROJECT_DATASET is required: the fixed dataset this deployment writes to");
  } else if (!DATASET_NAME.test(config.projectDataset)) {
    problems.push(`LOVABLE_PROJECT_DATASET ${JSON.stringify(config.projectDataset)} is not a valid dataset name (letters, digits, _ . - ; max 128)`);
  }
  for (const name of config.sharedDatasets) {
    if (!DATASET_NAME.test(name)) problems.push(`LOVABLE_SHARED_DATASETS entry ${JSON.stringify(name)} is not a valid dataset name`);
  }
  if (config.tokens.length === 0) {
    problems.push("LOVABLE_MCP_TOKEN is required: Lovable authenticates to this connector with a bearer token (generate one with `lovable-memory token`)");
  }
  for (const token of config.tokens) {
    if (token.length < 16) problems.push("LOVABLE_MCP_TOKEN entries must be at least 16 characters");
  }
  if (!["evidence", "answer"].includes(config.recall.defaultMode)) {
    problems.push(`LOVABLE_RECALL_MODE must be "evidence" or "answer" (got ${JSON.stringify(config.recall.defaultMode)})`);
  }
  if (config.recall.defaultMode === "answer" && !config.recall.allowAnswerMode) {
    problems.push("LOVABLE_RECALL_MODE=answer conflicts with LOVABLE_RECALL_ALLOW_ANSWER_MODE=false");
  }
  if (!["debug", "info", "warn", "error"].includes(config.logLevel)) {
    problems.push(`LOVABLE_LOG_LEVEL must be debug|info|warn|error (got ${JSON.stringify(config.logLevel)})`);
  }
  if (config.limits.defaultTopK < 1 || config.limits.defaultTopK > config.limits.maxRecallResults) {
    problems.push("LOVABLE_RECALL_TOP_K must be between 1 and LOVABLE_MAX_RECALL_RESULTS");
  }
  if (config.http.port < 0 || config.http.port > 65535) problems.push("LOVABLE_MCP_PORT must be 0-65535 (0 picks a free port)");
  if (config.http.publicUrl && !/^https?:\/\//.test(config.http.publicUrl)) {
    problems.push("LOVABLE_PUBLIC_URL must be an absolute http(s) URL");
  }
  if (problems.length) throw new ConfigError(problems.join("\n"));
}

/** A copy safe to print: secrets replaced, nothing else changed. */
export function redactConfig(config: LovableMemoryConfig): Record<string, unknown> {
  return {
    ...config,
    cognee: { ...config.cognee, apiKey: config.cognee.apiKey ? "<set>" : "<missing>" },
    tokens: config.tokens.map(() => "<set>"),
  };
}
