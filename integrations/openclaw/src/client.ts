import { createHash } from "node:crypto";
import type { CogneeAddResponse, CogneeDeleteMode, CogneeMode, CogneeSearchResult, CogneeSearchType } from "./types.js";

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

  async fetchJson<T>(
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

        if (response.status === 403) {
          const errorText = await response.text();
          console.warn(
            `cognee-openclaw: 403 Forbidden on ${path}. Response: ${errorText}. ` +
            `This typically means the authenticated user lacks permission on the target dataset. ` +
            `Enable permission grants in plugin config or check ENABLE_BACKEND_ACCESS_CONTROL on the Cognee server.`
          );
          throw new Error(`Cognee request forbidden (403): ${errorText}`);
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

    data = await this.fetchJson<CogneeAddResponse>(
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

    const data = await this.fetchJson<CogneeAddResponse>(
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
      const items = await this.fetchJson<DataItem[]>(path, { method: "GET" });
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
        await this.fetchJson<unknown>(`/datasets/${params.datasetId}/data/${params.dataId}`, { method: "DELETE" });
      } else {
        const query = new URLSearchParams({ data_id: params.dataId, dataset_id: params.datasetId, mode: params.mode ?? "soft" });
        await this.fetchJson<unknown>(`/api/v1/delete?${query.toString()}`, { method: "DELETE" });
      }
      return { datasetId: params.datasetId, dataId: params.dataId, deleted: true };
    } catch (error) {
      return { datasetId: params.datasetId, dataId: params.dataId, deleted: false, error: error instanceof Error ? error.message : String(error) };
    }
  }

  async cognify(params: { datasetIds?: string[] } = {}): Promise<{ status?: string }> {
    const path = this.isCloud ? "/cognify" : "/api/v1/cognify";
    return this.fetchJson<{ status?: string }>(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ datasetIds: params.datasetIds, runInBackground: true, temporal_cognify: true }),
    });
  }

  async memify(params: { datasetIds?: string[] } = {}): Promise<{ status?: string }> {
    const datasetId = params.datasetIds?.[0];
    return this.fetchJson<{ status?: string }>("/api/v1/memify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dataset_id: datasetId }),
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
    const data = await this.fetchJson<unknown>(searchPath, {
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

  async listDatasets(): Promise<{ id: string; name: string }[]> {
    const path = this.isCloud ? "/datasets" : "/api/v1/datasets";
    return this.fetchJson<{ id: string; name: string }[]>(path, { method: "GET" });
  }

  /**
   * Poll cognify pipeline status. Returns the status string ("completed", "running", "failed", etc.).
   */
  async datasetStatus(datasetId: string): Promise<string> {
    const path = this.isCloud ? `/datasets/status?dataset_id=${datasetId}` : `/api/v1/datasets/status?dataset_id=${datasetId}`;
    const resp = await this.fetchJson<Record<string, string>>(path, { method: "GET" });
    // Response is a dict keyed by dataset ID: { [datasetId]: "DATASET_PROCESSING_COMPLETED" }
    const status = resp[datasetId] ?? Object.values(resp)[0] ?? "unknown";
    return status.toLowerCase().replace("dataset_processing_", "");
  }

  // -- Permissions ------------------------------------------------------------

  async grantPermission(params: {
    datasetId: string;
    recipientId: string;
    permissionType?: string;
  }): Promise<{ granted: boolean; error?: string }> {
    const permissionName = params.permissionType ?? "read";
    // Cognee endpoint: POST /api/v1/permissions/datasets/{principal_id}
    // with query params: permission_name, dataset_ids
    const query = new URLSearchParams({
      permission_name: permissionName,
      dataset_ids: params.datasetId,
    });
    const endpoint = `/api/v1/permissions/datasets/${params.recipientId}?${query.toString()}`;
    try {
      await this.fetchJson(endpoint, {
        method: "POST",
      }, this.timeoutMs, 0); // 0 retries — permission grants are not critical path
      return { granted: true };
    } catch (error) {
      const msg = error instanceof Error ? error.message : String(error);
      // 404/405 means the endpoint doesn't exist on older Cognee versions
      if (msg.includes("404") || msg.includes("405") || msg.includes("Not Found") || msg.includes("Method Not Allowed")) {
        return { granted: false, error: "permission endpoint not available" };
      }
      // 409 likely means permission already exists — treat as success (idempotent)
      if (msg.includes("409") || msg.includes("Conflict") || msg.includes("already exists")) {
        return { granted: true };
      }
      return { granted: false, error: msg };
    }
  }

  async grantPermissions(params: {
    datasetId: string;
    recipientIds: string[];
    permissionType?: string;
    logger?: { info?: (msg: string) => void; warn?: (msg: string) => void };
  }): Promise<void> {
    if (params.recipientIds.length === 0) return;

    let endpointAvailable = true;
    for (const recipientId of params.recipientIds) {
      if (!endpointAvailable) break;

      const result = await this.grantPermission({
        datasetId: params.datasetId,
        recipientId,
        permissionType: params.permissionType,
      });

      if (result.granted) {
        params.logger?.info?.(`cognee-openclaw: granted ${params.permissionType ?? "read"} on dataset ${params.datasetId} to ${recipientId}`);
      } else if (result.error === "permission endpoint not available") {
        params.logger?.info?.("cognee-openclaw: permission endpoint not available on this Cognee version, skipping grants");
        endpointAvailable = false;
      } else {
        params.logger?.warn?.(`cognee-openclaw: failed to grant permission to ${recipientId}: ${result.error}`);
      }
    }
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
