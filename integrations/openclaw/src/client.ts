import { createHash } from "node:crypto";
import type { CogneeAddResponse, CogneeDeleteMode, CogneeMode, CogneeRememberItem, CogneeRememberResponse, CogneeSearchResult, CogneeSearchType } from "./types.js";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MAX_RETRIES = 2;
const RETRY_BASE_DELAY_MS = 3_000;
const DEFAULT_TIMEOUT_MS = 60_000;
const DEFAULT_INGESTION_TIMEOUT_MS = 300_000;

// ---------------------------------------------------------------------------
// CogneeHttpClient — shared HTTP transport with auth, retry, timeout
//
// Extracted so both the memory plugin and skills plugin can share one
// implementation instead of duplicating ~200 lines of fetch/auth logic.
// ---------------------------------------------------------------------------

export class CogneeHttpClient {
  private authToken: string | undefined;
  private loginPromise: Promise<void> | undefined;

  constructor(
    readonly baseUrl: string,
    private readonly apiKey?: string,
    private readonly username?: string,
    private readonly password?: string,
    private readonly timeoutMs: number = DEFAULT_TIMEOUT_MS,
    readonly ingestionTimeoutMs: number = DEFAULT_INGESTION_TIMEOUT_MS,
    readonly mode: CogneeMode = "local",
  ) { }

  private get isCloud(): boolean {
    return this.mode === "cloud";
  }

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

  async ensureAuth(): Promise<void> {
    if (this.isCloud) {
      if (!this.apiKey) throw new Error("Cognee Cloud mode requires an API key (set COGNEE_API_KEY)");
      return;
    }
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
    if (this.isCloud) {
      return { "X-Api-Key": this.apiKey! };
    }
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

  async fetchAPI<T>(
    path: string,
    init: RequestInit,
    timeoutMs = this.timeoutMs,
    responseParser: (r: Response) => Promise<T> = async (r: Response) => (await r.json()) as T,
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
            return responseParser(response);
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
      const headers = this.isCloud ? { "X-Api-Key": this.apiKey! } : {};
      const response = await fetch(`${this.baseUrl}/health`, {
        method: "GET",
        headers,
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
    filePath: string;
  }): Promise<{ datasetId: string; datasetName: string; dataId?: string }> {
    let data: CogneeAddResponse;

    const addPath = this.isCloud ? "/add" : "/api/v1/add";
    const formData = new FormData();
    const fileName = sanitizeFilePath(params.filePath);
    formData.append("data", new Blob([params.data], { type: "text/plain" }), fileName);
    formData.append("datasetName", params.datasetName);
    if (params.datasetId) {
      formData.append("datasetId", params.datasetId);
    }

    data = await this.fetchAPI<CogneeAddResponse>(
      addPath,
      { method: "POST", body: formData },
      this.ingestionTimeoutMs,
    );

    let dataId = extractDataId(data.data_id ?? data.data_ingestion_info);

    if (!dataId && data.dataset_id) {
      dataId = await this.resolveDataIdFromDataset(data.dataset_id, sanitizeFilePath(params.filePath));
    }

    if (!dataId) {
      console.warn(
        "cognee-openclaw: add response missing data_id and dataset lookup failed",
        JSON.stringify({ keys: Object.keys(data), data_id: data.data_id ?? null, data_ingestion_info: data.data_ingestion_info ?? null }, null, 2),
      );
    }

    return { datasetId: data.dataset_id, datasetName: data.dataset_name, dataId };
  }

  // POST /api/v1/remember — combines add + cognify (+ improve) in one call.
  // Cognee 1.0.3 introduced this as the recommended path for ingesting memory:
  // a single multipart upload of one or more files, the server runs the full
  // pipeline, and the response carries per-file `data_id`s under `items[]`.
  async remember(params: {
    files: { filePath: string; data: string }[];
    datasetName: string;
    datasetId?: string;
    sessionId?: string;
    nodeSet?: string[];
    runInBackground?: boolean;
    customPrompt?: string;
    chunksPerBatch?: number;
  }): Promise<{
    datasetId: string;
    datasetName: string;
    status?: string;
    pipelineRunId?: string;
    items: { filePath: string; uploadName: string; dataId?: string }[];
  }> {
    if (params.files.length === 0) {
      throw new Error("remember: at least one file is required");
    }

    const path = this.isCloud ? "/remember" : "/api/v1/remember";
    const formData = new FormData();
    const itemMappings: { filePath: string; uploadName: string }[] = [];

    for (const file of params.files) {
      const uploadName = sanitizeFilePath(file.filePath);
      formData.append("data", new Blob([file.data], { type: "text/plain" }), uploadName);
      itemMappings.push({ filePath: file.filePath, uploadName });
    }
    formData.append("datasetName", params.datasetName);
    if (params.datasetId) formData.append("datasetId", params.datasetId);
    if (params.sessionId) formData.append("session_id", params.sessionId);
    if (params.runInBackground) formData.append("run_in_background", "true");
    if (params.customPrompt) formData.append("custom_prompt", params.customPrompt);
    if (typeof params.chunksPerBatch === "number") {
      formData.append("chunks_per_batch", String(params.chunksPerBatch));
    }
    if (params.nodeSet && params.nodeSet.length > 0) {
      for (const node of params.nodeSet) formData.append("node_set", node);
    }

    const response = await this.fetchAPI<CogneeRememberResponse>(
      path,
      { method: "POST", body: formData },
      this.ingestionTimeoutMs,
    );

    const datasetId = response.dataset_id ?? params.datasetId ?? "";
    const datasetName = response.dataset_name ?? params.datasetName;
    const responseItems: CogneeRememberItem[] = Array.isArray(response.items) ? response.items : [];

    // Match each request file back to its response item by upload filename.
    // Cognee's ingestion pipeline derives `Data.name` from the upload's
    // filename via `Path(filename).stem`; our sanitizer already replaces
    // dots with dashes, so the stem equals the sanitized name.
    const itemsByName = new Map<string, CogneeRememberItem>();
    for (const item of responseItems) {
      if (item && typeof item.name === "string") {
        itemsByName.set(item.name, item);
      }
    }

    const items = await Promise.all(
      itemMappings.map(async ({ filePath, uploadName }) => {
        const matched = itemsByName.get(uploadName);
        let dataId = matched?.id;
        if (!dataId && datasetId) {
          dataId = await this.resolveDataIdFromDataset(datasetId, uploadName);
        }
        return { filePath, uploadName, dataId };
      }),
    );

    return {
      datasetId,
      datasetName,
      status: response.status,
      pipelineRunId: response.pipeline_run_id,
      items,
    };
  }

  async update(params: {
    dataId: string;
    datasetId: string;
    data: string;
    filePath: string;
    datasetName?: string;
  }): Promise<{ datasetId: string; datasetName: string; dataId?: string }> {
    if (this.isCloud) {
      // Cloud: update is not supported
      // Users should update data directly via the Cognee Cloud platform or API.
      return { datasetId: params.datasetId, datasetName: params.datasetName || params.datasetId, dataId: params.dataId };
    }

    // Local: PATCH /api/v1/update
    const query = new URLSearchParams({ data_id: params.dataId, dataset_id: params.datasetId });
    const formData = new FormData();
    const fileName = sanitizeFilePath(params.filePath);
    formData.append("data", new Blob([params.data], { type: "text/plain" }), fileName);

    const data = await this.fetchAPI<CogneeAddResponse>(
      `/api/v1/update?${query.toString()}`,
      { method: "PATCH", body: formData },
      this.ingestionTimeoutMs,
    );

    let dataId = extractDataId(data.data_id ?? data.data_ingestion_info);
    if (!dataId) {
      dataId = await this.resolveDataIdFromDataset(params.datasetId, sanitizeFilePath(params.filePath));
    }

    return { datasetId: data.dataset_id, datasetName: data.dataset_name, dataId };
  }

  async resolveDataIdFromDataset(datasetId: string, fileName: string): Promise<string | undefined> {
    try {
      const path = this.isCloud ? `/datasets/${datasetId}/data` : `/api/v1/datasets/${datasetId}/data`;
      type DataItem = { id: string; name: string };
      const items = await this.fetchAPI<DataItem[]>(path, { method: "GET" });
      if (!Array.isArray(items)) return undefined;
      const match = items.find((item) => item.name === fileName);
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
      if (this.isCloud) {
        // Cloud: DELETE /datasets/{datasetId}/data/{dataId}
        await this.fetchAPI<unknown>(`/datasets/${params.datasetId}/data/${params.dataId}`, { method: "DELETE" });
      } else {
        const query = new URLSearchParams({ data_id: params.dataId, dataset_id: params.datasetId, mode: params.mode ?? "soft" });
        await this.fetchAPI<unknown>(`/api/v1/delete?${query.toString()}`, { method: "DELETE" });
      }
      return { datasetId: params.datasetId, dataId: params.dataId, deleted: true };
    } catch (error) {
      return { datasetId: params.datasetId, dataId: params.dataId, deleted: false, error: error instanceof Error ? error.message : String(error) };
    }
  }

  // POST /api/v1/forget — unified deletion (per-item / per-dataset / everything).
  // Pass `dataset` as the name, not the UUID (cognee 1.0.3 type-coerces a
  // UUID-formatted string to str and falls through to a by-name lookup).
  // Cloud falls back to the per-item DELETE route.
  async forget(params: {
    dataId?: string;
    dataset?: string;
    everything?: boolean;
  }): Promise<{ datasetId?: string; dataId?: string; deleted: boolean; error?: string }> {
    try {
      if (this.isCloud) {
        if (!params.dataset || !params.dataId) {
          throw new Error("Cognee Cloud forget requires both dataset and dataId");
        }
        await this.fetchAPI<unknown>(`/datasets/${params.dataset}/data/${params.dataId}`, { method: "DELETE" });
        return { datasetId: params.dataset, dataId: params.dataId, deleted: true };
      }

      const body: Record<string, unknown> = {};
      if (params.everything) body.everything = true;
      if (params.dataset) body.dataset = params.dataset;
      if (params.dataId) body.data_id = params.dataId;

      await this.fetchAPI<unknown>("/api/v1/forget", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      return { datasetId: params.dataset, dataId: params.dataId, deleted: true };
    } catch (error) {
      return {
        datasetId: params.dataset,
        dataId: params.dataId,
        deleted: false,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }

  async cognify(params: { datasetIds?: string[] } = {}): Promise<{ status?: string }> {
    const path = this.isCloud ? "/cognify" : "/api/v1/cognify";
    return this.fetchAPI<{ status?: string }>(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ datasetIds: params.datasetIds, runInBackground: true, temporal_cognify: true }),
    });
  }

  async memify(params: { datasetIds?: string[] } = {}): Promise<{ status?: string }> {
    const datasetId = params.datasetIds?.[0];
    return this.fetchAPI<{ status?: string }>("/api/v1/memify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dataset_id: datasetId }),
    });
  }

  // POST /api/v1/improve — Cognee 1.0.3's memory-oriented alias for /memify.
  // Adds `session_ids` so callers can bridge session-cache content into the
  // permanent graph. /remember already runs improve server-side via
  // self_improvement=true, so the openclaw plugin doesn't need to call this
  // directly during normal sync — it's exposed for downstream consumers.
  async improve(params: {
    datasetId?: string;
    datasetName?: string;
    extractionTasks?: string[];
    enrichmentTasks?: string[];
    data?: string;
    nodeName?: string[];
    sessionIds?: string[];
    runInBackground?: boolean;
  }): Promise<{ status?: string }> {
    const path = this.isCloud ? "/improve" : "/api/v1/improve";
    return this.fetchAPI<{ status?: string }>(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...(params.datasetId ? { dataset_id: params.datasetId } : {}),
        ...(params.datasetName ? { dataset_name: params.datasetName } : {}),
        ...(params.extractionTasks ? { extraction_tasks: params.extractionTasks } : {}),
        ...(params.enrichmentTasks ? { enrichment_tasks: params.enrichmentTasks } : {}),
        ...(params.data ? { data: params.data } : {}),
        ...(params.nodeName ? { node_name: params.nodeName } : {}),
        ...(params.sessionIds ? { session_ids: params.sessionIds } : {}),
        ...(typeof params.runInBackground === "boolean" ? { run_in_background: params.runInBackground } : {}),
      }),
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
    const searchPath = this.isCloud ? "/search" : "/api/v1/search";
    const data = await this.fetchAPI<unknown>(searchPath, {
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
    return normalizeSearchResults(data);
  }

  // POST /api/v1/recall — Cognee 1.0.3's memory-oriented alias for /search.
  // Mirrors the search payload but adds session_id + scope, so results can
  // mix session-cache hits with graph hits when sessions are enabled.
  async recall(params: {
    queryText: string;
    searchPrompt: string;
    searchType: CogneeSearchType;
    datasetIds: string[];
    topK?: number;
    sessionId?: string;
    scope?: string | string[];
  }): Promise<CogneeSearchResult[]> {
    const recallPath = this.isCloud ? "/recall" : "/api/v1/recall";
    const data = await this.fetchAPI<unknown>(recallPath, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: params.queryText,
        search_type: params.searchType,
        dataset_ids: params.datasetIds,
        ...(typeof params.topK === "number" ? { top_k: params.topK } : {}),
        ...(params.searchPrompt ? { system_prompt: params.searchPrompt } : {}),
        ...(params.sessionId ? { session_id: params.sessionId } : {}),
        ...(params.scope ? { scope: params.scope } : {}),
      }),
    });
    return normalizeSearchResults(data);
  }

  async listDatasets(): Promise<{ id: string; name: string }[]> {
    const path = this.isCloud ? "/datasets" : "/api/v1/datasets";
    return this.fetchAPI<{ id: string; name: string }[]>(path, { method: "GET" });
  }

  async visualise(datasetId: string): Promise<unknown> {
    const path = this.isCloud
      ? `/visualize?dataset_id=${datasetId}`
      : `/api/v1/visualize?dataset_id=${datasetId}`;
    return this.fetchAPI<unknown>(
      path,
      { method: "GET" },
      this.timeoutMs,
      async (r: Response) => (await r.text()),
    );
  }

  /**
   * Poll cognify pipeline status. Returns the status string ("completed", "running", "failed", etc.).
   */
  async datasetStatus(datasetId: string): Promise<string> {
    // Cognee 1.0.3 renamed the query param from `dataset_id` to `dataset`.
    const path = this.isCloud ? `/datasets/status?dataset=${datasetId}` : `/api/v1/datasets/status?dataset=${datasetId}`;
    const resp = await this.fetchAPI<Record<string, string>>(path, { method: "GET" });
    // 1.0.3 returns lowercase enum values (e.g. "completed"); legacy responses used
    // "DATASET_PROCESSING_COMPLETED". The replace below normalizes both.
    const status = resp[datasetId] ?? Object.values(resp)[0] ?? "unknown";
    return status.toLowerCase().replace("dataset_processing_", "");
  }
}

// ---------------------------------------------------------------------------
// Helpers (module-private)
// ---------------------------------------------------------------------------

function sanitizeFilePath(filePath: string): string {
  var mutatedPath = filePath.replace(/\//g, '_');
  mutatedPath = mutatedPath.replace(/\./g, '-');
  return mutatedPath;
}

function extractDataId(value: unknown): string | undefined {
  if (!value) return undefined;
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    for (const entry of value) {
      const id = extractDataId(entry);
      if (id) return id;
    }
    return undefined;
  }
  if (typeof value !== "object") return undefined;
  const record = value as { data_id?: unknown; data_ingestion_info?: unknown };
  if (typeof record.data_id === "string") return record.data_id;
  return extractDataId(record.data_ingestion_info);
}

function normalizeSearchResults(data: unknown): CogneeSearchResult[] {
  if (Array.isArray(data)) {
    return data.map((item, index) => {
      if (typeof item === "string") {
        return { id: `result-${index}`, text: item, score: 1 };
      }
      if (item && typeof item === "object") {
        const record = item as Record<string, unknown>;

        // Extract text: prefer .text, then .search_result (cloud format), then stringify
        let text: string;
        if (typeof record.text === "string") {
          text = record.text;
        } else if (Array.isArray(record.search_result)) {
          text = record.search_result.map(String).join("\n");
        } else if (typeof record.search_result === "string") {
          text = record.search_result;
        } else {
          text = JSON.stringify(record);
        }

        return {
          id: typeof record.id === "string" ? record.id
            : typeof record.dataset_id === "string" ? record.dataset_id
              : `result-${index}`,
          text,
          score: typeof record.score === "number" ? record.score : 1,
          metadata: record.metadata as Record<string, unknown> | undefined,
        };
      }
      return { id: `result-${index}`, text: String(item), score: 1 };
    });
  }
  if (data && typeof data === "object" && "results" in data) {
    return normalizeSearchResults((data as { results: unknown }).results);
  }
  return [];
}
