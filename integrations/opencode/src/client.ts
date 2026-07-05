import type {
  CogneeAddResponse,
  CogneeDeleteMode,
  CogneeMode,
  CogneeRememberItem,
  CogneeRememberResponse,
  CogneeSearchResult,
  CogneeSearchType,
} from "./types.js";

const MAX_RETRIES = 2;
const RETRY_BASE_DELAY_MS = 3_000;
const DEFAULT_TIMEOUT_MS = 60_000;
const DEFAULT_INGESTION_TIMEOUT_MS = 300_000;

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
  ) {}

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
            return responseParser(retryResponse);
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

  async fetchJson<T>(
    path: string,
    init: RequestInit,
    timeoutMs = this.timeoutMs,
    responseParser: (r: Response) => Promise<T> = async (r: Response) => (await r.json()) as T,
    retries = MAX_RETRIES,
  ): Promise<T> {
    return this.fetchAPI<T>(path, init, timeoutMs, responseParser, retries);
  }

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

  async remember(params: {
    data: string;
    datasetName: string;
    datasetId?: string;
    sessionId?: string;
    nodeSet?: string[];
  }): Promise<{
    datasetId: string;
    datasetName: string;
    status?: string;
  }> {
    const path = this.isCloud ? "/remember" : "/api/v1/remember";
    const formData = new FormData();
    formData.append("data", new Blob([params.data], { type: "text/plain" }), "memory.txt");
    formData.append("datasetName", params.datasetName);
    if (params.datasetId) formData.append("datasetId", params.datasetId);
    if (params.sessionId) formData.append("session_id", params.sessionId);
    if (params.nodeSet && params.nodeSet.length > 0) {
      for (const node of params.nodeSet) formData.append("node_set", node);
    }

    const response = await this.fetchAPI<CogneeRememberResponse>(
      path,
      { method: "POST", body: formData },
      this.ingestionTimeoutMs,
    );

    return {
      datasetId: response.dataset_id ?? params.datasetId ?? "",
      datasetName: response.dataset_name ?? params.datasetName,
      status: response.status,
    };
  }

  async recall(params: {
    queryText: string;
    searchPrompt?: string;
    searchType?: CogneeSearchType;
    datasetIds: string[];
    topK?: number;
    sessionId?: string;
  }): Promise<CogneeSearchResult[]> {
    const recallPath = this.isCloud ? "/recall" : "/api/v1/recall";
    const data = await this.fetchAPI<unknown>(recallPath, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: params.queryText,
        search_type: params.searchType ?? "GRAPH_COMPLETION",
        dataset_ids: params.datasetIds,
        ...(typeof params.topK === "number" ? { top_k: params.topK } : {}),
        ...(params.searchPrompt ? { system_prompt: params.searchPrompt } : {}),
        ...(params.sessionId ? { session_id: params.sessionId } : {}),
      }),
    });
    return normalizeSearchResults(data);
  }

  async improve(params: {
    datasetId?: string;
    datasetName?: string;
    sessionIds?: string[];
  }): Promise<{ status?: string }> {
    const path = this.isCloud ? "/improve" : "/api/v1/improve";
    return this.fetchAPI<{ status?: string }>(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...(params.datasetId ? { dataset_id: params.datasetId } : {}),
        ...(params.datasetName ? { dataset_name: params.datasetName } : {}),
        ...(params.sessionIds ? { session_ids: params.sessionIds } : {}),
      }),
    });
  }

  async listDatasets(): Promise<{ id: string; name: string }[]> {
    const path = this.isCloud ? "/datasets" : "/api/v1/datasets";
    return this.fetchAPI<{ id: string; name: string }[]>(path, { method: "GET" });
  }
}

function normalizeSearchResults(data: unknown): CogneeSearchResult[] {
  if (Array.isArray(data)) {
    return data.map((item, index) => {
      if (typeof item === "string") {
        return { id: `result-${index}`, text: item, score: 1 };
      }
      if (item && typeof item === "object") {
        const record = item as Record<string, unknown>;
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
