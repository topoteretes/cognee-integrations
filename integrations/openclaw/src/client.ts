import { createHash } from "node:crypto";
import type {
  CogneeAddResponse,
  CogneeDataItem,
  CogneeDeleteMode,
  CogneeImproveResult,
  CogneeMode,
  CogneeRememberItem,
  CogneeRememberResponse,
  CogneeSearchResult,
  CogneeSearchType,
} from "./types.js";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MAX_RETRIES = 2;
const RETRY_BASE_DELAY_MS = 3_000;
const DEFAULT_TIMEOUT_MS = 120_000;
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
    private apiKey?: string,
    private readonly username?: string,
    private readonly password?: string,
    private readonly timeoutMs: number = DEFAULT_TIMEOUT_MS,
    readonly ingestionTimeoutMs: number = DEFAULT_INGESTION_TIMEOUT_MS,
    readonly mode: CogneeMode = "local",
  ) { }

  private get isCloud(): boolean {
    return this.mode === "cloud";
  }

  /**
   * Inject an API key resolved/minted after construction (resolveOrMintApiKey).
   * From then on every request authenticates with X-Api-Key and the JWT login
   * fallback is never used again — the key is the principal identity, matching
   * the claude-code/codex integrations. JWT login remains only as the one-time
   * bootstrap that mints a key on a fresh LOCAL server (cloud has no login
   * route, which is why COGNEE_API_KEY is mandatory there).
   */
  setApiKey(key: string): void {
    const trimmed = (key ?? "").trim();
    if (trimmed) this.apiKey = trimmed;
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
      // X-Api-Key ONLY — an API key is not a JWT, and servers that validate
      // Authorization as a JWT (e.g. cloud pods) can reject the request on a
      // bogus Bearer before the API key is even considered. Parity with the
      // claude-code/codex integrations, which send only X-Api-Key.
      return { "X-Api-Key": this.apiKey };
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
            return responseParser(retryResponse);
          } finally {
            clearTimeout(retryTimeout);
          }
        }

        if (!response.ok) {
          const errorText = await response.text();
          throw new Error(`Cognee request failed (${response.status}): ${errorText}`);
        }
        // Honor responseParser on the success path (gh #195, SDK-242) — the
        // `await` is load-bearing: it keeps a parser/body-read rejection (a
        // mid-read AbortError, or a parse error) inside this try so the catch
        // still clears the timer and retries on abort. Then clear the abort
        // timer on the resolve path too, so a leaked, still-armed timer doesn't
        // hold the Node event loop open until timeoutMs (SDK-215).
        const data = await responseParser(response);
        clearTimeout(timer);
        return data;
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

  // GET /api/v1/datasets/{id}/data — every stored document in a dataset.
  // DataDTO is camelCased on the wire (createdAt, mimeType, datasetId, …);
  // older servers may still emit snake_case, so both are accepted.
  async listDatasetData(datasetId: string): Promise<CogneeDataItem[]> {
    const path = this.isCloud ? `/datasets/${datasetId}/data` : `/api/v1/datasets/${datasetId}/data`;
    const items = await this.fetchAPI<unknown>(path, { method: "GET" });
    if (!Array.isArray(items)) return [];
    const out: CogneeDataItem[] = [];
    for (const raw of items) {
      if (!raw || typeof raw !== "object") continue;
      const r = raw as Record<string, unknown>;
      const id = typeof r.id === "string" ? r.id : undefined;
      if (!id) continue;
      const pick = (camel: string, snake: string): string | undefined => {
        const v = r[camel] ?? r[snake];
        return typeof v === "string" ? v : undefined;
      };
      const meta = r.externalMetadata ?? r.external_metadata;
      out.push({
        id,
        name: typeof r.name === "string" ? r.name : id,
        datasetId: pick("datasetId", "dataset_id") ?? datasetId,
        ...(pick("createdAt", "created_at") ? { createdAt: pick("createdAt", "created_at") } : {}),
        ...(pick("updatedAt", "updated_at") ? { updatedAt: pick("updatedAt", "updated_at") } : {}),
        ...(pick("mimeType", "mime_type") ? { mimeType: pick("mimeType", "mime_type") } : {}),
        ...(typeof r.extension === "string" ? { extension: r.extension } : {}),
        ...(typeof r.label === "string" ? { label: r.label } : {}),
        ...(meta && typeof meta === "object" ? { externalMetadata: meta as Record<string, unknown> } : {}),
      });
    }
    return out;
  }

  // GET /api/v1/datasets/{id}/data/{dataId}/raw — the original stored text
  // (FileResponse, so parsed as text, not JSON).
  async readRawData(datasetId: string, dataId: string, maxChars?: number): Promise<string> {
    const path = this.isCloud
      ? `/datasets/${datasetId}/data/${dataId}/raw`
      : `/api/v1/datasets/${datasetId}/data/${dataId}/raw`;
    const text = await this.fetchAPI<string>(path, { method: "GET" }, this.timeoutMs, async (r: Response) => await r.text());
    return typeof maxChars === "number" && text.length > maxChars ? text.slice(0, maxChars) : text;
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
  // Cloud now uses /forget as the primary route too, with a fallback to
  // legacy per-item DELETE for older deployments that don't expose /forget.
  async forget(params: {
    dataId?: string;
    /** Dataset NAME (server resolves by name). Mutually exclusive with datasetId. */
    dataset?: string;
    /** Dataset UUID. Preferred when known (e.g. from listDatasetData). */
    datasetId?: string;
    everything?: boolean;
  }): Promise<{ datasetId?: string; dataId?: string; deleted: boolean; error?: string }> {
    try {
      const body: Record<string, unknown> = {};
      if (params.everything) body.everything = true;
      if (params.datasetId) body.dataset_id = params.datasetId;
      else if (params.dataset) body.dataset = params.dataset;
      if (params.dataId) body.data_id = params.dataId;

      const forgetPath = this.isCloud ? "/forget" : "/api/v1/forget";
      try {
        await this.fetchAPI<unknown>(forgetPath, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } catch (error) {
        // Backward compatibility: older cloud deployments may not expose /forget.
        // In that case, fall back to per-item DELETE when enough identifiers are provided.
        const msg = error instanceof Error ? error.message : String(error);
        const missingForgetEndpoint = msg.includes("(404)") || msg.includes("(405)");
        const legacyDataset = params.datasetId ?? params.dataset;
        const canUseLegacyDelete = this.isCloud && !!legacyDataset && !!params.dataId;
        if (!missingForgetEndpoint || !canUseLegacyDelete) {
          throw error;
        }
        await this.fetchAPI<unknown>(`/datasets/${legacyDataset}/data/${params.dataId}`, {
          method: "DELETE",
        });
      }

      return { datasetId: params.datasetId ?? params.dataset, dataId: params.dataId, deleted: true };
    } catch (error) {
      return {
        datasetId: params.datasetId ?? params.dataset,
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
    const path = this.isCloud ? "/memify" : "/api/v1/memify";
    return this.fetchAPI<{ status?: string }>(path, {
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
  }): Promise<CogneeImproveResult> {
    const path = this.isCloud ? "/improve" : "/api/v1/improve";
    const data = await this.fetchAPI<unknown>(path, {
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
    return normalizeImproveResponse(data);
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
  //
  // only_context defaults to true (same as the claude-code/codex plugins):
  // the server returns the retrieved context and SKIPS the LLM completion
  // step, which dominates recall latency in the *_COMPLETION search types.
  // Injected memories should be stored context, not generated answers.
  async recall(params: {
    queryText: string;
    searchPrompt: string;
    searchType: CogneeSearchType;
    datasetIds: string[];
    topK?: number;
    sessionId?: string;
    /** Recall sources: "graph" | "session" | "trace" | "session_context" | "code" | "all" | "auto" or a list.
     *  Omitted = server "auto", which is graph-only whenever dataset_ids/search_type are set. */
    scope?: string | string[];
    /** "session_context" rendering profile: "qa" (conversational) or "agent" (tool/workflow). */
    contextProfile?: "qa" | "agent";
    /** "code" scope only: structured code-graph query ({operation, ...args}). Requires scope to include "code". */
    codeQuery?: Record<string, unknown>;
    onlyContext?: boolean;
    /** Per-call timeout for the prompt hot path. When set, retries are
     *  disabled so a slow server fails fast instead of eating the budget. */
    timeoutMs?: number;
  }): Promise<CogneeSearchResult[]> {
    const recallPath = this.isCloud ? "/recall" : "/api/v1/recall";
    const data = await this.fetchAPI<unknown>(
      recallPath,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: params.queryText,
          search_type: params.searchType,
          dataset_ids: params.datasetIds,
          only_context: params.onlyContext ?? true,
          ...(typeof params.topK === "number" ? { top_k: params.topK } : {}),
          ...(params.searchPrompt ? { system_prompt: params.searchPrompt } : {}),
          ...(params.sessionId ? { session_id: params.sessionId } : {}),
          ...(params.scope ? { scope: params.scope } : {}),
          ...(params.contextProfile ? { context_profile: params.contextProfile } : {}),
          ...(params.codeQuery ? { code_query: params.codeQuery } : {}),
        }),
      },
      params.timeoutMs ?? this.timeoutMs,
      undefined,
      params.timeoutMs ? 0 : undefined,
    );
    return normalizeSearchResults(data);
  }

  // POST /api/v1/remember/entry — store a typed QA/trace entry in the server's
  // session cache. Same contract as the claude-code/codex plugins'
  // remember_entry_via_http: body is { entry, dataset_name, session_id }.
  async rememberEntry(params: {
    datasetName: string;
    sessionId: string;
    entry: Record<string, unknown>;
  }): Promise<{ entryId?: string }> {
    const result = await this.fetchAPI<Record<string, unknown>>(
      "/api/v1/remember/entry",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          entry: params.entry,
          dataset_name: params.datasetName,
          session_id: params.sessionId,
        }),
      },
      30_000,
    );
    return { entryId: typeof result.entry_id === "string" ? result.entry_id : undefined };
  }

  async registerAgent(params: {
    agentSessionName: string;
    sessionId?: string;
    datasetNames?: string[];
  }): Promise<{ ok: boolean; connectionId?: string }> {
    const body: Record<string, unknown> = {
      agent_session_name: params.agentSessionName,
      // Self-declared connection type: "openclaw" is one of the server's
      // documented KNOWN_AGENT_CONNECTION_TYPES, so the dashboard attributes
      // this connection to the Openclaw plugin instead of generic API usage.
      type: "openclaw",
      memory_mode: "hybrid",
      source: "api",
    };
    if (params.sessionId) body.session_id = params.sessionId;
    if (params.datasetNames && params.datasetNames.length > 0) {
      body.dataset_names = params.datasetNames.filter((n) => n.trim());
    }
    const result = await this.fetchAPI<Record<string, unknown>>(
      "/api/v1/agents/register",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    );
    return { ok: true, connectionId: typeof result.id === "string" ? result.id : undefined };
  }

  async unregisterAgent(params: {
    agentSessionName: string;
  }): Promise<{ ok: boolean; activeAgents: number }> {
    try {
      const result = await this.fetchAPI<Record<string, unknown>>(
        "/api/v1/agents/unregister",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ agent_session_name: params.agentSessionName }),
        },
      );
      const raw = Number(result.activeAgents ?? result.active_agents ?? 0);
      const activeAgents = Number.isFinite(raw) ? raw : 0;
      return { ok: true, activeAgents };
    } catch (error) {
      return { ok: false, activeAgents: 0 };
    }
  }

  async listApiKeys(): Promise<{ key: string; name?: string }[]> {
    return this.fetchAPI<{ key: string; name?: string }[]>(
      "/api/v1/auth/api-keys",
      { method: "GET" },
    );
  }

  async createApiKey(name: string): Promise<{ key: string }> {
    return this.fetchAPI<{ key: string }>(
      "/api/v1/auth/api-keys",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      },
    );
  }

  /**
   * POST /datasets — create-or-return by name (the server is idempotent here).
   *
   * Exists for one reason: on Cognee <= 1.4.0 `improve(session_ids=…)` looks the
   * dataset up with an *existing-only* resolver and, when the name has never
   * been written to, swallows the failure as "non-fatal" — the session cache is
   * never bridged and the first session on a fresh dataset is lost, while the
   * trailing memify then creates the dataset so every later session works. Newer
   * servers resolve-or-create up front, where this is a harmless no-op.
   */
  async ensureDataset(name: string): Promise<string | undefined> {
    const path = this.isCloud ? "/datasets" : "/api/v1/datasets";
    const ds = await this.fetchAPI<{ id?: string }>(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    return typeof ds?.id === "string" ? ds.id : undefined;
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
  /**
   * POST /api/v1/remember with content_type="code": index one repository
   * (local path the server can read, or a git URL it clones) into a code-graph
   * dataset via the enola pipeline. No LLM/embedding calls unless
   * indexVectors. Requires cognee >= 1.5.3 (older servers reject content_type).
   */
  async indexRepository(params: {
    datasetName: string;
    repository: string;
    indexVectors?: boolean;
    runInBackground?: boolean;
  }): Promise<CogneeRememberResponse> {
    const path = this.isCloud ? "/remember" : "/api/v1/remember";
    const formData = new FormData();
    formData.append("datasetName", params.datasetName);
    formData.append("content_type", "code");
    formData.append("repositories", params.repository);
    formData.append("run_in_background", params.runInBackground === false ? "false" : "true");
    formData.append("index_vectors", params.indexVectors ? "true" : "false");
    return this.fetchAPI<CogneeRememberResponse>(path, { method: "POST", body: formData }, this.ingestionTimeoutMs);
  }

  /**
   * GET /api/v1/datasets/status?dataset=…&pipeline=… — status of one pipeline
   * for one dataset, lower-cased ("completed", "errored", "processing", …) or
   * "unknown". A single pipeline yields {id: status}; several yield
   * {id: {pipeline: status}}; both shapes are handled.
   */
  async pipelineStatus(datasetId: string, pipeline: string): Promise<string> {
    const q = `dataset=${encodeURIComponent(datasetId)}&pipeline=${encodeURIComponent(pipeline)}`;
    const path = this.isCloud ? `/datasets/status?${q}` : `/api/v1/datasets/status?${q}`;
    const resp = await this.fetchAPI<Record<string, unknown>>(path, { method: "GET" });
    let val: unknown = resp?.[datasetId];
    if (val === undefined && resp && Object.keys(resp).length === 1) val = Object.values(resp)[0];
    if (val && typeof val === "object") val = (val as Record<string, unknown>)[pipeline];
    return typeof val === "string" && val ? val.toLowerCase().replace("dataset_processing_", "") : "unknown";
  }

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

const RECALL_SOURCES: ReadonlySet<string> = new Set(["graph", "session", "trace", "session_context", "code", "tools", "system"]);

function asString(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "";
  try { return JSON.stringify(value); } catch { return String(value); }
}

/**
 * Render one recall entry to text according to its `source` (RecallResponse
 * discriminator). Session layers don't carry `text`: Q&A turns have
 * question/answer, trace steps have origin_function/status/return_value,
 * session_context has content.
 */
function recallEntryText(record: Record<string, unknown>, source: string | undefined): string {
  if (typeof record.text === "string") return record.text;
  if (source === "session" || (typeof record.question === "string" && typeof record.answer === "string")) {
    const q = asString(record.question).trim();
    const a = asString(record.answer).trim();
    const fb = typeof record.feedback_text === "string" && record.feedback_text.trim() ? `\nFeedback: ${record.feedback_text.trim()}` : "";
    return `Q: ${q}\nA: ${a}${fb}`;
  }
  if (source === "trace" || typeof record.origin_function === "string") {
    const fn = asString(record.origin_function);
    const status = asString(record.status) || "success";
    const params = record.method_params !== undefined && record.method_params !== null ? ` params=${asString(record.method_params).slice(0, 300)}` : "";
    const ret = record.return_value !== undefined && record.return_value !== null ? `\nreturned: ${asString(record.return_value).slice(0, 500)}` : "";
    const fb = typeof record.feedback_text === "string" && record.feedback_text.trim() ? `\nLesson: ${record.feedback_text.trim()}` : "";
    return `${fn} (${status})${params}${ret}${fb}`;
  }
  if (typeof record.content === "string") return record.content;
  if (Array.isArray(record.search_result)) return record.search_result.map(String).join("\n"); // cloud format
  if (typeof record.search_result === "string") return record.search_result;
  return JSON.stringify(record);
}

export function normalizeSearchResults(data: unknown): CogneeSearchResult[] {
  if (Array.isArray(data)) {
    return data.map((item, index) => {
      if (typeof item === "string") {
        return { id: `result-${index}`, text: item, score: 1 };
      }
      if (item && typeof item === "object") {
        const record = item as Record<string, unknown>;
        const rawSource = typeof record.source === "string" ? record.source
          : typeof record._source === "string" ? record._source : undefined;
        const source = rawSource && RECALL_SOURCES.has(rawSource) ? (rawSource as CogneeSearchResult["source"]) : undefined;

        return {
          id: typeof record.id === "string" ? record.id
            : typeof record.entry_id === "string" ? record.entry_id
              : typeof record.dataset_id === "string" ? record.dataset_id
                : `result-${index}`,
          text: recallEntryText(record, source),
          score: typeof record.score === "number" ? record.score : 1,
          metadata: record.metadata as Record<string, unknown> | undefined,
          ...(source ? { source } : {}),
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

/**
 * Collapse the `/improve` response to one shape. Cognee >= 1.4 answers
 * `{ "<dataset_uuid>": { status, pipeline_run_id, ... }, ... }`; older servers
 * a flat `{ status, ... }`. Reading `.status` off the map yielded undefined
 * and the plugin logged `status=?` on every session end.
 */
export function normalizeImproveResponse(data: unknown): CogneeImproveResult {
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    return { error: `unexpected improve response: ${data === null ? "null" : Array.isArray(data) ? "array" : typeof data}` };
  }
  const record = data as Record<string, unknown>;

  const flatStatus = typeof record.status === "string" ? record.status : undefined;
  const flatRun = typeof record.pipeline_run_id === "string" ? record.pipeline_run_id : undefined;
  if (flatStatus !== undefined || flatRun !== undefined) {
    return {
      ...(flatStatus !== undefined ? { status: flatStatus } : {}),
      ...(flatRun !== undefined ? { pipelineRunId: flatRun } : {}),
      ...(typeof record.dataset_id === "string" ? { datasetId: record.dataset_id } : {}),
    };
  }

  const entries = Object.entries(record).filter(([, v]) => v && typeof v === "object" && !Array.isArray(v));
  if (entries.length === 0) {
    const keys = Object.keys(record);
    return { error: `unexpected improve response: object with keys [${keys.slice(0, 8).join(", ")}${keys.length > 8 ? ", …" : ""}]` };
  }

  const datasets: NonNullable<CogneeImproveResult["datasets"]> = {};
  for (const [dsId, v] of entries) {
    const inner = v as Record<string, unknown>;
    datasets[dsId] = {
      ...(typeof inner.status === "string" ? { status: inner.status } : {}),
      ...(typeof inner.pipeline_run_id === "string" ? { pipelineRunId: inner.pipeline_run_id } : {}),
    };
  }
  const statuses = new Set(Object.values(datasets).map((d) => d.status).filter((s): s is string => typeof s === "string"));
  const status = statuses.size === 1 ? [...statuses][0] : statuses.size > 1 ? "mixed" : undefined;
  const single = entries.length === 1 ? datasets[entries[0][0]] : undefined;
  return {
    ...(status !== undefined ? { status } : {}),
    ...(single?.pipelineRunId ? { pipelineRunId: single.pipelineRunId } : {}),
    ...(entries.length === 1 ? { datasetId: entries[0][0] } : {}),
    datasets,
  };
}
