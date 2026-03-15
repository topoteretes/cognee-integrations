import { promises as fs } from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { homedir } from "node:os";
import { basename, dirname, join, relative, resolve } from "node:path";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type CogneeSearchType =
  | "GRAPH_COMPLETION"
  | "GRAPH_COMPLETION_COT"
  | "GRAPH_COMPLETION_CONTEXT_EXTENSION"
  | "GRAPH_SUMMARY_COMPLETION"
  | "RAG_COMPLETION"
  | "TRIPLET_COMPLETION"
  | "CHUNKS"
  | "CHUNKS_LEXICAL"
  | "SUMMARIES"
  | "CYPHER"
  | "NATURAL_LANGUAGE"
  | "TEMPORAL"
  | "CODING_RULES"
  | "FEELING_LUCKY";

type CogneeDeleteMode = "soft" | "hard";

type MemoryScope = "company" | "user" | "agent";

type ScopeRoute = {
  /** Glob-style pattern matched against the file's relative path */
  pattern: string;
  /** Target memory scope */
  scope: MemoryScope;
};

type CogneePluginConfig = {
  baseUrl?: string;
  apiKey?: string;
  username?: string;
  password?: string;

  // --- Legacy flat dataset (still supported as fallback) ---
  datasetName?: string;

  // --- Multi-scope memory ---
  companyDataset?: string;
  userDatasetPrefix?: string;
  agentDatasetPrefix?: string;
  userId?: string;
  agentId?: string;
  recallScopes?: MemoryScope[];
  defaultWriteScope?: MemoryScope;
  scopeRouting?: ScopeRoute[];

  // --- Session ---
  enableSessions?: boolean;
  persistSessionsAfterEnd?: boolean;

  // --- Search ---
  searchType?: CogneeSearchType;
  searchPrompt?: string;
  deleteMode?: CogneeDeleteMode;
  maxResults?: number;
  minScore?: number;
  maxTokens?: number;

  // --- Automation ---
  autoRecall?: boolean;
  autoIndex?: boolean;
  autoCognify?: boolean;
  autoMemify?: boolean;

  // --- Timeouts ---
  requestTimeoutMs?: number;
  ingestionTimeoutMs?: number;
};

type CogneeAddResponse = {
  dataset_id: string;
  dataset_name: string;
  message: string;
  data_id?: unknown;
  data_ingestion_info?: unknown;
};

type CogneeSearchResult = {
  id: string;
  text: string;
  score: number;
  metadata?: Record<string, unknown>;
};

type CogneeSearchResponse = {
  results: CogneeSearchResult[];
};

type DatasetState = Record<string, string>;

type SyncIndex = {
  datasetId?: string;
  datasetName?: string;
  entries: Record<string, { hash: string; dataId?: string }>;
};

/** Per-scope sync indexes, keyed by scope name */
type ScopedSyncIndexes = Record<string, SyncIndex>;

type MemoryFile = {
  /** Relative path from workspace root (e.g. "MEMORY.md", "memory/tools.md") */
  path: string;
  /** Absolute path on disk */
  absPath: string;
  /** File content */
  content: string;
  /** SHA-256 hex hash of content */
  hash: string;
};

type SyncResult = {
  added: number;
  updated: number;
  skipped: number;
  errors: number;
  deleted: number;
};

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

const DEFAULT_BASE_URL = "http://localhost:8000";
const DEFAULT_DATASET_NAME = "openclaw";
const DEFAULT_SEARCH_TYPE: CogneeSearchType = "FEELING_LUCKY";
const DEFAULT_SEARCH_PROMPT = "";
const DEFAULT_DELETE_MODE: CogneeDeleteMode = "soft";
const DEFAULT_MAX_RESULTS = 6;
const DEFAULT_MIN_SCORE = 0;
const DEFAULT_MAX_TOKENS = 512;
const DEFAULT_AUTO_RECALL = true;
const DEFAULT_AUTO_INDEX = true;
const DEFAULT_AUTO_COGNIFY = true;
const DEFAULT_AUTO_MEMIFY = false;
const DEFAULT_REQUEST_TIMEOUT_MS = 60_000;
const DEFAULT_INGESTION_TIMEOUT_MS = 300_000; // 5 min for add/update (ingestion is slow)
const MAX_RETRIES = 2;
const RETRY_BASE_DELAY_MS = 3_000;

const DEFAULT_RECALL_SCOPES: MemoryScope[] = ["agent", "user", "company"];
const DEFAULT_WRITE_SCOPE: MemoryScope = "agent";
const DEFAULT_SCOPE_ROUTING: ScopeRoute[] = [
  { pattern: "memory/company/**", scope: "company" },
  { pattern: "memory/company/*", scope: "company" },
  { pattern: "memory/user/**", scope: "user" },
  { pattern: "memory/user/*", scope: "user" },
  { pattern: "memory/**", scope: "agent" },
  { pattern: "memory/*", scope: "agent" },
  { pattern: "MEMORY.md", scope: "agent" },
];

const STATE_DIR = join(homedir(), ".openclaw", "memory", "cognee");
const STATE_PATH = join(STATE_DIR, "datasets.json");
const SYNC_INDEX_PATH = join(STATE_DIR, "sync-index.json");
const SCOPED_SYNC_INDEX_PATH = join(STATE_DIR, "scoped-sync-indexes.json");

/** Glob patterns for memory files, relative to workspace root. */
const MEMORY_FILE_PATTERNS = ["MEMORY.md", "memory"];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function resolveEnvVars(value: string): string {
  return value.replace(/\$\{([^}]+)\}/g, (_, envVar) => {
    const envValue = process.env[envVar];
    if (!envValue) {
      throw new Error(`Environment variable ${envVar} is not set`);
    }
    return envValue;
  });
}

function hashText(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

/**
 * Minimal glob matcher — supports `*` (single segment) and `**` (any depth).
 * Operates on forward-slash-normalized relative paths.
 */
function matchGlob(pattern: string, filePath: string): boolean {
  const regexStr = pattern
    .replace(/\*\*/g, "<<GLOBSTAR>>")
    .replace(/\*/g, "[^/]*")
    .replace(/<<GLOBSTAR>>/g, ".*");
  return new RegExp(`^${regexStr}$`).test(filePath);
}

function routeFileToScope(
  filePath: string,
  routes: ScopeRoute[],
  defaultScope: MemoryScope,
): MemoryScope {
  const normalized = filePath.replace(/\\/g, "/");
  for (const route of routes) {
    if (matchGlob(route.pattern, normalized)) {
      return route.scope;
    }
  }
  return defaultScope;
}

function resolveConfig(rawConfig: unknown): Required<CogneePluginConfig> {
  const raw =
    rawConfig && typeof rawConfig === "object" && !Array.isArray(rawConfig)
      ? (rawConfig as CogneePluginConfig)
      : {};

  const baseUrl = raw.baseUrl?.trim() || DEFAULT_BASE_URL;
  const datasetName = raw.datasetName?.trim() || DEFAULT_DATASET_NAME;
  const searchType = raw.searchType || DEFAULT_SEARCH_TYPE;
  const searchPrompt = raw.searchPrompt || DEFAULT_SEARCH_PROMPT;
  const deleteMode = raw.deleteMode === "hard" ? "hard" : DEFAULT_DELETE_MODE;
  const maxResults =
    typeof raw.maxResults === "number" ? raw.maxResults : DEFAULT_MAX_RESULTS;
  const minScore =
    typeof raw.minScore === "number" ? raw.minScore : DEFAULT_MIN_SCORE;
  const maxTokens =
    typeof raw.maxTokens === "number" ? raw.maxTokens : DEFAULT_MAX_TOKENS;
  const autoRecall =
    typeof raw.autoRecall === "boolean" ? raw.autoRecall : DEFAULT_AUTO_RECALL;
  const autoIndex =
    typeof raw.autoIndex === "boolean" ? raw.autoIndex : DEFAULT_AUTO_INDEX;
  const autoCognify =
    typeof raw.autoCognify === "boolean" ? raw.autoCognify : DEFAULT_AUTO_COGNIFY;
  const autoMemify =
    typeof raw.autoMemify === "boolean" ? raw.autoMemify : DEFAULT_AUTO_MEMIFY;
  const requestTimeoutMs =
    typeof raw.requestTimeoutMs === "number" ? raw.requestTimeoutMs : DEFAULT_REQUEST_TIMEOUT_MS;
  const ingestionTimeoutMs =
    typeof raw.ingestionTimeoutMs === "number" ? raw.ingestionTimeoutMs : DEFAULT_INGESTION_TIMEOUT_MS;

  const apiKey =
    raw.apiKey && raw.apiKey.length > 0
      ? resolveEnvVars(raw.apiKey)
      : process.env.COGNEE_API_KEY || "";

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
  const enableSessions =
    typeof raw.enableSessions === "boolean" ? raw.enableSessions : true;
  const persistSessionsAfterEnd =
    typeof raw.persistSessionsAfterEnd === "boolean" ? raw.persistSessionsAfterEnd : true;

  return {
    baseUrl,
    apiKey,
    username,
    password,
    datasetName,
    companyDataset,
    userDatasetPrefix,
    agentDatasetPrefix,
    userId,
    agentId,
    recallScopes,
    defaultWriteScope,
    scopeRouting,
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
    requestTimeoutMs,
    ingestionTimeoutMs,
  };
}

/**
 * Determine whether multi-scope mode is active.
 * It's active when at least one scope-specific dataset prefix/name is configured.
 */
function isMultiScopeEnabled(cfg: Required<CogneePluginConfig>): boolean {
  return !!(cfg.companyDataset || cfg.userDatasetPrefix || cfg.agentDatasetPrefix);
}

/**
 * Resolve the Cognee dataset name for a given memory scope.
 */
function datasetNameForScope(scope: MemoryScope, cfg: Required<CogneePluginConfig>): string {
  switch (scope) {
    case "company":
      return cfg.companyDataset || `${cfg.datasetName}-company`;
    case "user":
      return cfg.userDatasetPrefix
        ? `${cfg.userDatasetPrefix}-${cfg.userId || "default"}`
        : `${cfg.datasetName}-user-${cfg.userId || "default"}`;
    case "agent":
      return cfg.agentDatasetPrefix
        ? `${cfg.agentDatasetPrefix}-${cfg.agentId || "default"}`
        : `${cfg.datasetName}-agent-${cfg.agentId || "default"}`;
  }
}

// ---------------------------------------------------------------------------
// Persistence — dataset state & sync index
// ---------------------------------------------------------------------------

async function loadDatasetState(): Promise<DatasetState> {
  try {
    const raw = await fs.readFile(STATE_PATH, "utf-8");
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    return parsed as DatasetState;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
}

async function saveDatasetState(state: DatasetState): Promise<void> {
  await fs.mkdir(dirname(STATE_PATH), { recursive: true });
  await fs.writeFile(STATE_PATH, JSON.stringify(state, null, 2), "utf-8");
}

async function loadSyncIndex(): Promise<SyncIndex> {
  try {
    const raw = await fs.readFile(SYNC_INDEX_PATH, "utf-8");
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") {
      return { entries: {} };
    }
    const record = parsed as SyncIndex;
    record.entries ??= {};
    return record;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return { entries: {} };
    }
    throw error;
  }
}

async function saveSyncIndex(state: SyncIndex): Promise<void> {
  await fs.mkdir(dirname(SYNC_INDEX_PATH), { recursive: true });
  await fs.writeFile(SYNC_INDEX_PATH, JSON.stringify(state, null, 2), "utf-8");
}

async function loadScopedSyncIndexes(): Promise<ScopedSyncIndexes> {
  try {
    const raw = await fs.readFile(SCOPED_SYNC_INDEX_PATH, "utf-8");
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    return parsed as ScopedSyncIndexes;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
}

async function saveScopedSyncIndexes(indexes: ScopedSyncIndexes): Promise<void> {
  await fs.mkdir(dirname(SCOPED_SYNC_INDEX_PATH), { recursive: true });
  await fs.writeFile(SCOPED_SYNC_INDEX_PATH, JSON.stringify(indexes, null, 2), "utf-8");
}

// ---------------------------------------------------------------------------
// File collection — scan workspace for memory markdown files
// ---------------------------------------------------------------------------

async function collectMemoryFiles(workspaceDir: string): Promise<MemoryFile[]> {
  const files: MemoryFile[] = [];

  for (const pattern of MEMORY_FILE_PATTERNS) {
    const target = resolve(workspaceDir, pattern);

    try {
      const stat = await fs.stat(target);

      if (stat.isFile() && target.endsWith(".md")) {
        const content = await fs.readFile(target, "utf-8");
        files.push({
          path: relative(workspaceDir, target),
          absPath: target,
          content,
          hash: hashText(content),
        });
      } else if (stat.isDirectory()) {
        const entries = await scanDir(target, workspaceDir);
        files.push(...entries);
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
        throw error;
      }
    }
  }

  return files;
}

async function scanDir(dir: string, workspaceDir: string): Promise<MemoryFile[]> {
  const files: MemoryFile[] = [];

  const entries = await fs.readdir(dir, { withFileTypes: true });
  for (const entry of entries) {
    const absPath = join(dir, entry.name);

    if (entry.isDirectory()) {
      const nested = await scanDir(absPath, workspaceDir);
      files.push(...nested);
    } else if (entry.isFile() && entry.name.endsWith(".md")) {
      const content = await fs.readFile(absPath, "utf-8");
      files.push({
        path: relative(workspaceDir, absPath),
        absPath,
        content,
        hash: hashText(content),
      });
    }
  }

  return files;
}

// ---------------------------------------------------------------------------
// Cognee HTTP client
// ---------------------------------------------------------------------------

class CogneeClient {
  private authToken: string | undefined;
  private loginPromise: Promise<void> | undefined;

  constructor(
    private readonly baseUrl: string,
    private readonly apiKey?: string,
    private readonly username?: string,
    private readonly password?: string,
    private readonly timeoutMs: number = 30_000,
    private readonly ingestionTimeoutMs: number = DEFAULT_INGESTION_TIMEOUT_MS,
  ) {}

  /**
   * Authenticate with Cognee via /api/v1/auth/login.
   * Falls back to default local dev credentials when none are configured.
   */
  async login(): Promise<void> {
    const user = this.username || "default_user@example.com";
    const pass = this.password || "default_password";

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await fetch(`${this.baseUrl}/api/v1/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ username: user, password: pass }),
        signal: controller.signal,
      });
      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`Cognee login failed (${response.status}): ${errorText}`);
      }
      const data = (await response.json()) as { access_token?: string; token?: string };
      this.authToken = data.access_token ?? data.token;
      if (!this.authToken) {
        throw new Error("Cognee login succeeded but no token in response");
      }
    } finally {
      clearTimeout(timeout);
    }
  }

  /**
   * Ensure the client is authenticated (login once, reuse token).
   */
  async ensureAuth(): Promise<void> {
    if (this.authToken || this.apiKey) return;
    if (!this.loginPromise) {
      this.loginPromise = this.login().catch((err) => {
        this.loginPromise = undefined;
        throw err;
      });
    }
    return this.loginPromise;
  }

  private buildHeaders(): Record<string, string> {
    if (this.apiKey) {
      return {
        Authorization: `Bearer ${this.apiKey}`,
        "X-Api-Key": this.apiKey,
      };
    }
    if (this.authToken) {
      return { Authorization: `Bearer ${this.authToken}` };
    }
    return {};
  }

  private async fetchJson<T>(
    path: string,
    init: RequestInit,
    timeoutMs = this.timeoutMs,
    retries = MAX_RETRIES,
  ): Promise<T> {
    await this.ensureAuth();

    let lastError: unknown;
    for (let attempt = 0; attempt <= retries; attempt++) {
      if (attempt > 0) {
        const delay = RETRY_BASE_DELAY_MS * 2 ** (attempt - 1);
        await new Promise((r) => setTimeout(r, delay));
      }

      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetch(`${this.baseUrl}${path}`, {
          ...init,
          headers: { ...this.buildHeaders(), ...(init.headers as Record<string, string>) },
          signal: controller.signal,
        });

        // On 401, try re-login once and retry
        if (response.status === 401 && !this.apiKey) {
          clearTimeout(timer);
          this.authToken = undefined;
          this.loginPromise = undefined;
          await this.ensureAuth();

          const retryController = new AbortController();
          const retryTimeout = setTimeout(() => retryController.abort(), timeoutMs);
          try {
            const retryResponse = await fetch(`${this.baseUrl}${path}`, {
              ...init,
              headers: { ...this.buildHeaders(), ...(init.headers as Record<string, string>) },
              signal: retryController.signal,
            });
            if (!retryResponse.ok) {
              const errorText = await retryResponse.text();
              throw new Error(`Cognee request failed (${retryResponse.status}): ${errorText}`);
            }
            return (await retryResponse.json()) as T;
          } finally {
            clearTimeout(retryTimeout);
          }
        }

        if (!response.ok) {
          const errorText = await response.text();
          throw new Error(`Cognee request failed (${response.status}): ${errorText}`);
        }
        return (await response.json()) as T;
      } catch (error) {
        clearTimeout(timer);
        const isTimeout =
          error instanceof DOMException ||
          (error instanceof Error && error.name === "AbortError");
        if (isTimeout && attempt < retries) {
          lastError = error;
          continue;
        }
        throw error;
      }
    }
    throw lastError;
  }

  // -- Health ---------------------------------------------------------------

  async health(): Promise<{ status: string }> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await fetch(`${this.baseUrl}/health`, {
        method: "GET",
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(`Cognee health check failed (${response.status})`);
      }
      return (await response.json()) as { status: string };
    } finally {
      clearTimeout(timer);
    }
  }

  // -- Data operations ------------------------------------------------------

  async add(params: {
    data: string;
    datasetName: string;
    datasetId?: string;
  }): Promise<{ datasetId: string; datasetName: string; dataId?: string }> {
    const formData = new FormData();
    const hash8 = createHash("sha256").update(params.data).digest("hex").slice(0, 8);
    const fileName = `openclaw-memory-${hash8}.txt`;
    formData.append("data", new Blob([params.data], { type: "text/plain" }), fileName);
    formData.append("datasetName", params.datasetName);
    if (params.datasetId) {
      formData.append("datasetId", params.datasetId);
    }

    const data = await this.fetchJson<CogneeAddResponse>(
      "/api/v1/add",
      { method: "POST", body: formData },
      this.ingestionTimeoutMs,
    );

    let dataId = this.extractDataId(data.data_id ?? data.data_ingestion_info);

    if (!dataId && data.dataset_id) {
      dataId = await this.resolveDataIdFromDataset(data.dataset_id, fileName);
    }

    if (!dataId) {
      console.warn(
        "cognee-openclaw: add response missing data_id and dataset lookup failed",
        JSON.stringify(
          {
            keys: Object.keys(data),
            data_id: data.data_id ?? null,
            data_ingestion_info: data.data_ingestion_info ?? null,
          },
          null,
          2,
        ),
      );
    }

    return {
      datasetId: data.dataset_id,
      datasetName: data.dataset_name,
      dataId,
    };
  }

  async update(params: {
    dataId: string;
    datasetId: string;
    data: string;
  }): Promise<{ datasetId: string; datasetName: string; dataId?: string }> {
    const query = new URLSearchParams({
      data_id: params.dataId,
      dataset_id: params.datasetId,
    });

    const formData = new FormData();
    const hash8 = createHash("sha256").update(params.data).digest("hex").slice(0, 8);
    const fileName = `openclaw-memory-${hash8}.txt`;
    formData.append("data", new Blob([params.data], { type: "text/plain" }), fileName);

    const data = await this.fetchJson<CogneeAddResponse>(
      `/api/v1/update?${query.toString()}`,
      { method: "PATCH", body: formData },
      this.ingestionTimeoutMs,
    );

    let dataId = this.extractDataId(data.data_id ?? data.data_ingestion_info);

    if (!dataId) {
      dataId = await this.resolveDataIdFromDataset(params.datasetId, fileName);
    }

    return {
      datasetId: data.dataset_id,
      datasetName: data.dataset_name,
      dataId,
    };
  }

  /**
   * Query the dataset's data items and find a matching entry by filename.
   * Used as fallback when add/update responses don't include a usable data_id.
   */
  async resolveDataIdFromDataset(datasetId: string, fileName: string): Promise<string | undefined> {
    try {
      type DataItem = { id: string; name: string };
      const items = await this.fetchJson<DataItem[]>(`/api/v1/datasets/${datasetId}/data`, {
        method: "GET",
      });
      if (!Array.isArray(items)) return undefined;
      const match = items.find((item) => item.name === fileName.replace(/\.txt$/, ""));
      return match?.id;
    } catch {
      return undefined;
    }
  }

  async delete(params: {
    dataId: string;
    datasetId: string;
    mode?: CogneeDeleteMode;
  }): Promise<{ datasetId: string; dataId: string; deleted: boolean; error?: string }> {
    try {
      const query = new URLSearchParams({
        data_id: params.dataId,
        dataset_id: params.datasetId,
        mode: params.mode ?? "soft",
      });
      await this.fetchJson<unknown>(`/api/v1/delete?${query.toString()}`, {
        method: "DELETE",
      });
      return {
        datasetId: params.datasetId,
        dataId: params.dataId,
        deleted: true,
      };
    } catch (error) {
      return {
        datasetId: params.datasetId,
        dataId: params.dataId,
        deleted: false,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }

  async cognify(params: { datasetIds?: string[] } = {}): Promise<{ status?: string }> {
    return this.fetchJson<{ status?: string }>("/api/v1/cognify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ datasetIds: params.datasetIds, runInBackground: true }),
    });
  }

  async memify(params: { datasetIds?: string[] } = {}): Promise<{ status?: string }> {
    return this.fetchJson<{ status?: string }>("/api/v1/memify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ datasetIds: params.datasetIds }),
    });
  }

  async search(params: {
    queryText: string;
    searchPrompt: string;
    searchType: CogneeSearchType;
    datasetIds: string[];
    maxTokens: number;
    sessionId?: string;
  }): Promise<CogneeSearchResult[]> {
    const data = await this.fetchJson<unknown>("/api/v1/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: params.queryText,
        searchType: params.searchType,
        datasetIds: params.datasetIds,
        max_tokens: params.maxTokens,
        ...(params.searchPrompt ? { systemPrompt: params.searchPrompt } : {}),
        ...(params.sessionId ? { session_id: params.sessionId } : {}),
      }),
    });

    return this.normalizeSearchResults(data);
  }

  /**
   * Normalize Cognee search response to consistent format.
   * Cognee returns a direct array of strings: ["answer text here"]
   * We convert to: [{ id, text, score }]
   */
  private normalizeSearchResults(data: unknown): CogneeSearchResult[] {
    // Handle direct array (Cognee's actual format)
    if (Array.isArray(data)) {
      return data.map((item, index) => {
        if (typeof item === "string") {
          return { id: `result-${index}`, text: item, score: 1 };
        }
        if (item && typeof item === "object") {
          const record = item as Record<string, unknown>;
          return {
            id: typeof record.id === "string" ? record.id : `result-${index}`,
            text: typeof record.text === "string" ? record.text : JSON.stringify(record),
            score: typeof record.score === "number" ? record.score : 1,
            metadata: record.metadata as Record<string, unknown> | undefined,
          };
        }
        return { id: `result-${index}`, text: String(item), score: 1 };
      });
    }

    // Handle wrapped format { results: [...] }
    if (data && typeof data === "object" && "results" in data) {
      return this.normalizeSearchResults((data as { results: unknown }).results);
    }

    return [];
  }

  // -- Datasets -------------------------------------------------------------

  async listDatasets(): Promise<{ id: string; name: string }[]> {
    return this.fetchJson<{ id: string; name: string }[]>("/api/v1/datasets", {
      method: "GET",
    });
  }

  private extractDataId(value: unknown): string | undefined {
    if (!value) return undefined;
    if (typeof value === "string") return value;
    if (Array.isArray(value)) {
      for (const entry of value) {
        const id = this.extractDataId(entry);
        if (id) return id;
      }
      return undefined;
    }
    if (typeof value !== "object") return undefined;
    const record = value as { data_id?: unknown; data_ingestion_info?: unknown };
    if (typeof record.data_id === "string") return record.data_id;
    return this.extractDataId(record.data_ingestion_info);
  }
}

// ---------------------------------------------------------------------------
// Unified sync logic
//
// For each memory file:
//   - New file (no sync index entry)        -> add + cognify
//   - Changed file with dataId              -> update (no re-cognify)
//   - Changed file without dataId           -> add + cognify
//   - Unchanged file                        -> skip
//   - Deleted file (in index, not on disk)  -> delete + cognify
//
// Based on clawdbot cognee-provider.ts syncFiles(), extended with delete support.
// ---------------------------------------------------------------------------

async function syncFiles(
  client: CogneeClient,
  changedFiles: MemoryFile[],
  fullFiles: MemoryFile[],
  syncIndex: SyncIndex,
  cfg: Required<CogneePluginConfig>,
  logger: { info?: (msg: string) => void; warn?: (msg: string) => void },
  /** Override dataset name for scoped sync */
  overrideDatasetName?: string,
): Promise<SyncResult & { datasetId?: string }> {
  const result: SyncResult = { added: 0, updated: 0, skipped: 0, errors: 0, deleted: 0 };
  const dsName = overrideDatasetName || cfg.datasetName;
  let datasetId = syncIndex.datasetId;
  let needsCognify = false;

  for (const file of changedFiles) {
    const existing = syncIndex.entries[file.path];

    // Skip unchanged files
    if (existing && existing.hash === file.hash) {
      result.skipped++;
      continue;
    }

    const dataWithMetadata = `# ${file.path}\n\n${file.content}\n\n---\nMetadata: ${JSON.stringify({ path: file.path, source: "memory" })}`;

    try {
      // Changed file with prior dataId -> try update first
      if (existing?.dataId && datasetId) {
        try {
          const updateResponse = await client.update({
            dataId: existing.dataId,
            datasetId,
            data: dataWithMetadata,
          });

          const newDataId = updateResponse.dataId;
          if (!newDataId) {
            logger.warn?.(`cognee-openclaw: update for ${file.path} succeeded but could not resolve new data_id`);
          }
          syncIndex.entries[file.path] = { hash: file.hash, dataId: newDataId };
          syncIndex.datasetId = datasetId;
          syncIndex.datasetName = dsName;
          result.updated++;

          logger.info?.(`cognee-openclaw: updated ${file.path}`);
          continue; // Success, move to next file
        } catch (updateError) {
          // If update fails (404/409 - document not found), fall back to add
          const errorMsg = updateError instanceof Error ? updateError.message : String(updateError);
          if (errorMsg.includes("404") || errorMsg.includes("409") || errorMsg.includes("not found")) {
            logger.info?.(`cognee-openclaw: update failed for ${file.path}, falling back to add`);
            // Clear the stale dataId and fall through to add
            delete existing.dataId;
          } else {
            throw updateError; // Re-throw other errors
          }
        }
      }

      // New file, or changed file without dataId, or update failed -> add
      const response = await client.add({
        data: dataWithMetadata,
        datasetName: dsName,
        datasetId,
      });

      if (response.datasetId && response.datasetId !== datasetId) {
        datasetId = response.datasetId;

        // Persist dataset ID mapping
        const state = await loadDatasetState();
        state[dsName] = response.datasetId;
        await saveDatasetState(state);
      }

      syncIndex.entries[file.path] = {
        hash: file.hash,
        dataId: response.dataId,
      };
      syncIndex.datasetId = datasetId;
      syncIndex.datasetName = dsName;
      needsCognify = true;
      result.added++;

      logger.info?.(`cognee-openclaw: added ${file.path}`);
    } catch (error) {
      result.errors++;
      logger.warn?.(`cognee-openclaw: failed to sync ${file.path}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  // Handle deletions: remove from Cognee any files no longer present
  const currentPaths = new Set(fullFiles.map(f => f.path));
  for (const [path, entry] of Object.entries(syncIndex.entries)) {
    if (!currentPaths.has(path) && entry.dataId && datasetId) {
      const deleteResult = await client.delete({
        dataId: entry.dataId,
        datasetId,
        mode: cfg.deleteMode,
      });
      if (deleteResult.deleted) {
        result.deleted++;
        delete syncIndex.entries[path];
        logger.info?.(`cognee-openclaw: deleted ${path}`);
      } else {
        const isNotFound = deleteResult.error && (
          deleteResult.error.includes("404") || deleteResult.error.includes("409") || deleteResult.error.includes("not found")
        );
        if (isNotFound) {
          result.deleted++;
          delete syncIndex.entries[path];
          logger.info?.(`cognee-openclaw: deleted ${path} (already removed from Cognee)`);
        } else {
          result.errors++;
          logger.warn?.(`cognee-openclaw: failed to delete ${path}${deleteResult.error ? `: ${deleteResult.error}` : ""}`);
        }
      }
    }
  }

  // Cognify only after adds (updates re-process inline; hard-deletes clean up graph nodes inline)
  if (needsCognify && cfg.autoCognify && datasetId) {
    try {
      await client.cognify({ datasetIds: [datasetId] });
      logger.info?.("cognee-openclaw: cognify dispatched");

      // Optionally run memify after cognify
      if (cfg.autoMemify) {
        try {
          await client.memify({ datasetIds: [datasetId] });
          logger.info?.("cognee-openclaw: memify dispatched");
        } catch (error) {
          logger.warn?.(`cognee-openclaw: memify failed: ${error instanceof Error ? error.message : String(error)}`);
        }
      }
    } catch (error) {
      logger.warn?.(`cognee-openclaw: cognify failed: ${error instanceof Error ? error.message : String(error)}`);
    }
  }

  // Save sync index to disk
  await saveSyncIndex(syncIndex);

  return { ...result, datasetId };
}

// ---------------------------------------------------------------------------
// Multi-scope sync logic
// ---------------------------------------------------------------------------

/**
 * Group files by memory scope and sync each scope's files to its own dataset.
 */
async function syncFilesScoped(
  client: CogneeClient,
  changedFiles: MemoryFile[],
  fullFiles: MemoryFile[],
  scopedIndexes: ScopedSyncIndexes,
  cfg: Required<CogneePluginConfig>,
  logger: { info?: (msg: string) => void; warn?: (msg: string) => void },
): Promise<SyncResult & { datasetIds: Record<MemoryScope, string | undefined> }> {
  const totalResult: SyncResult = { added: 0, updated: 0, skipped: 0, errors: 0, deleted: 0 };
  const datasetIds: Record<MemoryScope, string | undefined> = {
    company: undefined,
    user: undefined,
    agent: undefined,
  };

  // Group changed files by scope
  const changedByScope = new Map<MemoryScope, MemoryFile[]>();
  for (const file of changedFiles) {
    const scope = routeFileToScope(file.path, cfg.scopeRouting, cfg.defaultWriteScope);
    const list = changedByScope.get(scope) ?? [];
    list.push(file);
    changedByScope.set(scope, list);
  }

  // Group all files by scope (needed for deletion detection)
  const fullByScope = new Map<MemoryScope, MemoryFile[]>();
  for (const file of fullFiles) {
    const scope = routeFileToScope(file.path, cfg.scopeRouting, cfg.defaultWriteScope);
    const list = fullByScope.get(scope) ?? [];
    list.push(file);
    fullByScope.set(scope, list);
  }

  // Determine which scopes need processing (have changes or tracked entries)
  const allScopes = new Set<MemoryScope>([
    ...changedByScope.keys(),
    ...(Object.keys(scopedIndexes) as MemoryScope[]),
  ]);

  for (const scope of allScopes) {
    const dsName = datasetNameForScope(scope, cfg);
    const scopeChanged = changedByScope.get(scope) ?? [];
    const scopeFull = fullByScope.get(scope) ?? [];

    // Load or initialize sync index for this scope
    if (!scopedIndexes[scope]) {
      scopedIndexes[scope] = { entries: {} };
    }
    const scopeIndex = scopedIndexes[scope];

    // Check for deletions in this scope
    const currentPaths = new Set(scopeFull.map(f => f.path));
    const hasDeletedFiles = Object.keys(scopeIndex.entries).some(p => !currentPaths.has(p));

    if (scopeChanged.length === 0 && !hasDeletedFiles) continue;

    logger.info?.(`cognee-openclaw: [${scope}] syncing ${scopeChanged.length} changed file(s) to dataset "${dsName}"${hasDeletedFiles ? " + deletions" : ""}`);

    const result = await syncFiles(
      client,
      scopeChanged,
      scopeFull,
      scopeIndex,
      cfg,
      logger,
      dsName,
    );

    totalResult.added += result.added;
    totalResult.updated += result.updated;
    totalResult.skipped += result.skipped;
    totalResult.errors += result.errors;
    totalResult.deleted += result.deleted;
    datasetIds[scope] = result.datasetId;
  }

  // Persist all scoped indexes
  await saveScopedSyncIndexes(scopedIndexes);

  return { ...totalResult, datasetIds };
}

// ---------------------------------------------------------------------------
// Plugin registration
// ---------------------------------------------------------------------------

const memoryCogneePlugin = {
  id: "cognee-openclaw",
  name: "Memory (Cognee)",
  description: "Cognee-backed memory with multi-scope support (company/user/agent), session tracking, and auto-recall",
  kind: "memory" as const,
  register(api: OpenClawPluginApi) {
    const cfg = resolveConfig(api.pluginConfig);
    const client = new CogneeClient(cfg.baseUrl, cfg.apiKey, cfg.username, cfg.password, cfg.requestTimeoutMs, cfg.ingestionTimeoutMs);
    const multiScope = isMultiScopeEnabled(cfg);

    // Legacy single-scope state
    let datasetId: string | undefined;
    let syncIndex: SyncIndex = { entries: {} };

    // Multi-scope state
    let scopedIndexes: ScopedSyncIndexes = {};

    // Session state
    let sessionId: string | undefined;

    let resolvedWorkspaceDir: string | undefined;

    // Load persisted state on startup
    const stateReady = Promise.all([
      loadDatasetState()
        .then((state) => {
          if (multiScope) {
            // For multi-scope, we populate datasetIds from state per scope
          } else {
            datasetId = state[cfg.datasetName];
          }
        })
        .catch((error) => {
          api.logger.warn?.(`cognee-openclaw: failed to load dataset state: ${String(error)}`);
        }),
      multiScope
        ? loadScopedSyncIndexes()
            .then((indexes) => {
              scopedIndexes = indexes;
            })
            .catch((error) => {
              api.logger.warn?.(`cognee-openclaw: failed to load scoped sync indexes: ${String(error)}`);
            })
        : loadSyncIndex()
            .then((state) => {
              syncIndex = state;
              if (!datasetId && state.datasetId && state.datasetName === cfg.datasetName) {
                datasetId = state.datasetId;
              }
            })
            .catch((error) => {
              api.logger.warn?.(`cognee-openclaw: failed to load sync index: ${String(error)}`);
            }),
    ]);

    // Helper: resolve dataset IDs for recall scopes
    async function getRecallDatasetIds(): Promise<string[]> {
      const state = await loadDatasetState();
      const ids: string[] = [];

      if (multiScope) {
        for (const scope of cfg.recallScopes) {
          const dsName = datasetNameForScope(scope, cfg);
          const dsId = state[dsName] ?? scopedIndexes[scope]?.datasetId;
          if (dsId) ids.push(dsId);
        }
      } else {
        if (datasetId) ids.push(datasetId);
      }

      return ids;
    }

    // Helper: run sync with a given workspace dir
    async function runSync(workspaceDir: string, logger: { info?: (msg: string) => void; warn?: (msg: string) => void }) {
      await stateReady;

      const files = await collectMemoryFiles(workspaceDir);
      if (files.length === 0) {
        logger.info?.("cognee-openclaw: no memory files found");
        return { added: 0, updated: 0, skipped: 0, errors: 0, deleted: 0 };
      }

      logger.info?.(`cognee-openclaw: found ${files.length} memory file(s), syncing...`);

      if (multiScope) {
        const result = await syncFilesScoped(client, files, files, scopedIndexes, cfg, logger);
        return result;
      } else {
        const result = await syncFiles(client, files, files, syncIndex, cfg, logger);
        if (result.datasetId) {
          datasetId = result.datasetId;
        }
        return result;
      }
    }

    // ------------------------------------------------------------------
    // CLI: openclaw cognee index / openclaw cognee status / openclaw cognee health
    // ------------------------------------------------------------------

    api.registerCli((ctx) => {
      const cognee = ctx.program.command("cognee").description("Cognee memory management");
      const resolvedWorkspaceDir = ctx.workspaceDir || process.cwd();

      cognee
        .command("index")
        .description("Sync memory files to Cognee (add new, update changed, skip unchanged)")
        .action(async () => {
          const result = await runSync(resolvedWorkspaceDir, ctx.logger);
          const summary = `Sync complete: ${result.added} added, ${result.updated} updated, ${result.deleted} deleted, ${result.skipped} unchanged, ${result.errors} errors`;
          ctx.logger.info?.(summary);
          console.log(summary);
        });

      cognee
        .command("status")
        .description("Show Cognee sync state (files indexed, dataset info)")
        .action(async () => {
          await stateReady;

          const files = await collectMemoryFiles(resolvedWorkspaceDir);

          if (multiScope) {
            const state = await loadDatasetState();
            for (const scope of (["company", "user", "agent"] as MemoryScope[])) {
              const dsName = datasetNameForScope(scope, cfg);
              const scopeIndex = scopedIndexes[scope] ?? { entries: {} };
              const entryCount = Object.keys(scopeIndex.entries).length;

              const scopeFiles = files.filter(f =>
                routeFileToScope(f.path, cfg.scopeRouting, cfg.defaultWriteScope) === scope
              );

              let dirty = 0;
              let newCount = 0;
              for (const file of scopeFiles) {
                const existing = scopeIndex.entries[file.path];
                if (!existing) newCount++;
                else if (existing.hash !== file.hash) dirty++;
              }

              console.log(`\n[${scope.toUpperCase()}] Dataset: ${dsName}`);
              console.log(`  Dataset ID: ${state[dsName] ?? scopeIndex.datasetId ?? "(not set)"}`);
              console.log(`  Indexed files: ${entryCount}`);
              console.log(`  Workspace files: ${scopeFiles.length}`);
              console.log(`  New (unindexed): ${newCount}`);
              console.log(`  Changed (dirty): ${dirty}`);
            }
          } else {
            const entryCount = Object.keys(syncIndex.entries).length;
            const entriesWithDataId = Object.values(syncIndex.entries).filter((e) => e.dataId).length;

            let dirty = 0;
            let newCount = 0;
            for (const file of files) {
              const existing = syncIndex.entries[file.path];
              if (!existing) newCount++;
              else if (existing.hash !== file.hash) dirty++;
            }

            const lines = [
              `Dataset: ${syncIndex.datasetName ?? cfg.datasetName}`,
              `Dataset ID: ${datasetId ?? syncIndex.datasetId ?? "(not set)"}`,
              `Indexed files: ${entryCount} (${entriesWithDataId} with data ID)`,
              `Workspace files: ${files.length}`,
              `New (unindexed): ${newCount}`,
              `Changed (dirty): ${dirty}`,
              `Sync index: ${SYNC_INDEX_PATH}`,
            ];
            console.log(lines.join("\n"));
          }
        });

      cognee
        .command("health")
        .description("Check Cognee API connectivity")
        .action(async () => {
          try {
            const result = await client.health();
            console.log(`Cognee API: OK (${cfg.baseUrl})`);
            if (result.status) console.log(`Status: ${result.status}`);
          } catch (error) {
            console.log(`Cognee API: UNREACHABLE (${cfg.baseUrl})`);
            console.log(`Error: ${error instanceof Error ? error.message : String(error)}`);
            process.exitCode = 1;
          }
        });

      cognee
        .command("scopes")
        .description("Show memory scope routing for current workspace files")
        .action(async () => {
          const files = await collectMemoryFiles(resolvedWorkspaceDir);
          if (files.length === 0) {
            console.log("No memory files found.");
            return;
          }

          if (!multiScope) {
            console.log(`Multi-scope mode is OFF. All files go to dataset "${cfg.datasetName}".`);
            console.log(`Set companyDataset, userDatasetPrefix, or agentDatasetPrefix to enable.`);
            return;
          }

          const grouped: Record<MemoryScope, string[]> = { company: [], user: [], agent: [] };
          for (const file of files) {
            const scope = routeFileToScope(file.path, cfg.scopeRouting, cfg.defaultWriteScope);
            grouped[scope].push(file.path);
          }

          for (const scope of (["company", "user", "agent"] as MemoryScope[])) {
            const dsName = datasetNameForScope(scope, cfg);
            console.log(`\n[${scope.toUpperCase()}] -> dataset "${dsName}"`);
            if (grouped[scope].length === 0) {
              console.log("  (no files)");
            } else {
              for (const p of grouped[scope]) {
                console.log(`  ${p}`);
              }
            }
          }
        });
    }, { commands: ["cognee"] });

    // ------------------------------------------------------------------
    // Auto-sync on startup (with health check)
    // ------------------------------------------------------------------

    if (cfg.autoIndex) {
      api.registerService({
        id: "cognee-auto-sync",
        async start(ctx) {
          // Store workspace dir for use in hooks
          resolvedWorkspaceDir = ctx.workspaceDir || process.cwd();

          // Health check before attempting sync
          try {
            await client.health();
          } catch (error) {
            ctx.logger.warn?.(`cognee-openclaw: Cognee API unreachable at ${cfg.baseUrl} — auto-sync disabled for this session. Error: ${String(error)}`);
            return;
          }

          // Initialize session
          if (cfg.enableSessions) {
            sessionId = `openclaw-${randomUUID()}`;
            ctx.logger.info?.(`cognee-openclaw: session ${sessionId}`);
          }

          try {
            const result = await runSync(resolvedWorkspaceDir, ctx.logger);
            ctx.logger.info?.(
              `cognee-openclaw: auto-sync complete: ${result.added} added, ${result.updated} updated, ${result.deleted} deleted, ${result.skipped} unchanged`,
            );
          } catch (error) {
            ctx.logger.warn?.(`cognee-openclaw: auto-sync failed: ${String(error)}`);
          }
        },
      });
    }

    // ------------------------------------------------------------------
    // Auto-recall: inject memories before each agent run
    // ------------------------------------------------------------------

    if (cfg.autoRecall) {
      api.on("before_agent_start", async (event, ctx) => {
        // Wait for state to load (fixes race condition on first agent run)
        await stateReady;

        if (!event.prompt || event.prompt.length < 5) {
          api.logger.debug?.("cognee-openclaw: skipping recall (prompt too short)");
          return;
        }

        const recallDatasetIds = await getRecallDatasetIds();
        if (recallDatasetIds.length === 0) {
          api.logger.debug?.("cognee-openclaw: skipping recall (no datasetIds)");
          return;
        }

        try {
          if (multiScope) {
            // Multi-scope: search each scope independently for labeled results
            const scopeResults: Record<string, CogneeSearchResult[]> = {};
            const state = await loadDatasetState();

            const searchPromises = cfg.recallScopes.map(async (scope) => {
              const dsName = datasetNameForScope(scope, cfg);
              const dsId = state[dsName] ?? scopedIndexes[scope]?.datasetId;
              if (!dsId) return;

              try {
                const results = await client.search({
                  queryText: event.prompt,
                  searchType: cfg.searchType,
                  datasetIds: [dsId],
                  searchPrompt: cfg.searchPrompt,
                  maxTokens: cfg.maxTokens,
                  sessionId,
                });

                const filtered = results
                  .filter((r) => r.score >= cfg.minScore)
                  .slice(0, cfg.maxResults);

                if (filtered.length > 0) {
                  scopeResults[scope] = filtered;
                }
              } catch (error) {
                api.logger.warn?.(`cognee-openclaw: recall failed for scope ${scope}: ${error instanceof Error ? error.message : String(error)}`);
              }
            });

            await Promise.all(searchPromises);

            if (Object.keys(scopeResults).length === 0) {
              api.logger.debug?.("cognee-openclaw: search returned no results above minScore");
              return;
            }

            // Build labeled context
            const sections: string[] = [];
            for (const scope of cfg.recallScopes) {
              const results = scopeResults[scope];
              if (!results || results.length === 0) continue;
              const payload = JSON.stringify(
                results.map((r) => ({ id: r.id, score: r.score, text: r.text, metadata: r.metadata })),
                null,
                2,
              );
              sections.push(`<${scope}_memory>\n${payload}\n</${scope}_memory>`);
            }

            const totalResults = Object.values(scopeResults).reduce((sum, arr) => sum + arr.length, 0);
            api.logger.info?.(
              `cognee-openclaw: injecting ${totalResults} memories across ${Object.keys(scopeResults).length} scope(s) for session ${ctx.sessionKey ?? "unknown"}`,
            );

            return {
              prependContext: `<cognee_memories>\n${sections.join("\n")}\n</cognee_memories>`,
            };
          } else {
            // Legacy single-scope search
            const results = await client.search({
              queryText: event.prompt,
              searchType: cfg.searchType,
              datasetIds: recallDatasetIds,
              searchPrompt: cfg.searchPrompt,
              maxTokens: cfg.maxTokens,
              sessionId,
            });

            const filtered = results
              .filter((result) => result.score >= cfg.minScore)
              .slice(0, cfg.maxResults);

            if (filtered.length === 0) {
              api.logger.debug?.("cognee-openclaw: search returned no results above minScore");
              return;
            }

            const payload = JSON.stringify(
              filtered.map((result) => ({
                id: result.id,
                score: result.score,
                text: result.text,
                metadata: result.metadata,
              })),
              null,
              2,
            );

            api.logger.info?.(
              `cognee-openclaw: injecting ${filtered.length} memories for session ${ctx.sessionKey ?? "unknown"}`,
            );

            return {
              prependContext: `<cognee_memories>\nRelevant memories:\n${payload}\n</cognee_memories>`,
            };
          }
        } catch (error) {
          api.logger.warn?.(`cognee-openclaw: recall failed: ${String(error)}`);
        }
      });
    }

    // ------------------------------------------------------------------
    // Post-agent sync: detect file changes and sync to Cognee
    // ------------------------------------------------------------------

    if (cfg.autoIndex) {
      api.on("agent_end", async (event, ctx) => {
        // Only sync if the agent succeeded
        if (!event.success) return;

        await stateReady;

        // Need workspace dir to find memory files
        const workspaceDir = resolvedWorkspaceDir || process.cwd();

        try {
          if (multiScope) {
            // Reload scoped indexes from disk
            try {
              const freshIndexes = await loadScopedSyncIndexes();
              scopedIndexes = freshIndexes;
            } catch {
              // Fall through with existing in-memory indexes
            }

            const files = await collectMemoryFiles(workspaceDir);

            // Check if any scope has changes
            let hasChanges = false;
            for (const file of files) {
              const scope = routeFileToScope(file.path, cfg.scopeRouting, cfg.defaultWriteScope);
              const scopeIndex = scopedIndexes[scope];
              if (!scopeIndex) { hasChanges = true; break; }
              const existing = scopeIndex.entries[file.path];
              if (!existing || existing.hash !== file.hash) { hasChanges = true; break; }
            }

            // Check for deletions across all scopes
            if (!hasChanges) {
              const currentPaths = new Set(files.map(f => f.path));
              for (const scopeIndex of Object.values(scopedIndexes)) {
                if (Object.keys(scopeIndex.entries).some(p => !currentPaths.has(p))) {
                  hasChanges = true;
                  break;
                }
              }
            }

            if (!hasChanges) return;

            api.logger.info?.("cognee-openclaw: detected changes, syncing across scopes...");
            const result = await syncFilesScoped(client, files, files, scopedIndexes, cfg, api.logger);

            api.logger.info?.(
              `cognee-openclaw: post-agent sync: ${result.added} added, ${result.updated} updated, ${result.deleted} deleted`,
            );
          } else {
            // Legacy single-scope
            // Reload sync index from disk to pick up changes made by CLI or other processes
            try {
              const freshIndex = await loadSyncIndex();
              syncIndex.entries = freshIndex.entries;
              if (freshIndex.datasetId) syncIndex.datasetId = freshIndex.datasetId;
              if (freshIndex.datasetName) syncIndex.datasetName = freshIndex.datasetName;
            } catch {
              // Fall through with existing in-memory index
            }

            // Collect current files and find changed ones
            const files = await collectMemoryFiles(workspaceDir);
            const changedFiles = files.filter((f) => {
              const existing = syncIndex.entries[f.path];
              return !existing || existing.hash !== f.hash;
            });

            // Check for deletions: files tracked in the sync index but no longer on disk
            const currentPaths = new Set(files.map(f => f.path));
            const hasDeletedFiles = Object.keys(syncIndex.entries).some(p => !currentPaths.has(p));

            if (changedFiles.length === 0 && !hasDeletedFiles) return;

            api.logger.info?.(`cognee-openclaw: detected ${changedFiles.length} changed file(s)${hasDeletedFiles ? " + deletions" : ""}, syncing...`);

            const result = await syncFiles(client, changedFiles, files, syncIndex, cfg, api.logger);
            if (result.datasetId) {
              datasetId = result.datasetId;
            }

            api.logger.info?.(
              `cognee-openclaw: post-agent sync: ${result.added} added, ${result.updated} updated, ${result.deleted} deleted`,
            );
          }
        } catch (error) {
          api.logger.warn?.(`cognee-openclaw: post-agent sync failed: ${String(error)}`);
        }
      });
    }
  },
};

export default memoryCogneePlugin;

// Exports for testing
export { CogneeClient, syncFiles, syncFilesScoped, matchGlob, routeFileToScope, datasetNameForScope, isMultiScopeEnabled };
export type {
  CogneeDeleteMode,
  CogneePluginConfig,
  CogneeSearchType,
  MemoryFile,
  MemoryScope,
  ScopeRoute,
  ScopedSyncIndexes,
  SyncIndex,
  SyncResult,
};
