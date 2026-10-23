/**
 * The only module that talks to cognee. Wire contract verified live against
 * cognee 1.6.5 (fixtures in `fixtures/cognee-1.6.5/`):
 *
 *   POST /api/v1/datasets                       {name} → DatasetDTO (create-or-return)
 *   GET  /api/v1/datasets                       → DatasetDTO[]
 *   POST /api/v1/remember                       multipart data=<file>, datasetName,
 *                                               run_in_background → {status, dataset_id, ...}
 *   POST /api/v1/recall                         {query, datasets, scope:["graph"],
 *                                               search_type, top_k, include_references}
 *   GET  /api/v1/datasets/status?dataset=&pipeline=cognify_pipeline
 *   GET  /api/v1/datasets/{id}/data?limit=&offset=
 *   POST /api/v1/forget                         {datasetId, dataId}
 *   GET  /health
 *
 * Auth is `X-Api-Key` (cognee API keys, self-hosted and Cloud). JWT login is
 * deliberately not implemented: the connector is a long-running service with
 * one operator-held key, and a login flow would need the default user's
 * password on the server (cognee >= 1.6.0 ships without one).
 */
import { CogneeApiError } from "./errors.js";

export interface CogneeClientOptions {
  baseUrl: string;
  apiKey: string;
  requestTimeoutMs: number;
  maxResponseBytes: number;
  fetch?: typeof fetch;
}

export interface RequestOptions {
  timeoutMs?: number;
  /** Retries for idempotent reads only; writes never retry here. */
  retries?: number;
}

export interface DatasetDTO {
  id: string;
  name: string;
  [key: string]: unknown;
}

export interface DataItemDTO {
  id: string;
  name: string;
  datasetId?: string;
  createdAt?: string;
  updatedAt?: string | null;
  [key: string]: unknown;
}

export interface RememberResponse {
  status?: string;
  dataset_id?: string | null;
  dataset_name?: string | null;
  pipeline_run_id?: string | null;
  [key: string]: unknown;
}

export interface RecallItem {
  kind?: string;
  search_type?: string;
  text?: string;
  score?: number | null;
  dataset_id?: string | null;
  dataset_name?: string | null;
  metadata?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export type SearchType = "CHUNKS" | "GRAPH_COMPLETION";

export interface RecallRequest {
  query: string;
  datasets: string[];
  searchType: SearchType;
  topK: number;
  includeReferences?: boolean;
}

export interface RememberRequest {
  datasetName: string;
  /** Upload filename: the document's identity server-side. Content-derived. */
  filename: string;
  text: string;
}

const RETRY_BASE_DELAY_MS = 500;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class CogneeClient {
  readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly requestTimeoutMs: number;
  private readonly maxResponseBytes: number;
  private readonly fetchImpl: typeof fetch;

  constructor(options: CogneeClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.apiKey = options.apiKey;
    this.requestTimeoutMs = options.requestTimeoutMs;
    this.maxResponseBytes = options.maxResponseBytes;
    this.fetchImpl = options.fetch ?? fetch;
  }

  // -- health / datasets ----------------------------------------------------

  /** GET /health — no auth. Returns the body on any status; throws only on transport failure. */
  async health(opts: RequestOptions = {}): Promise<{ reachable: boolean; status: number; body: unknown }> {
    const { status, body } = await this.rawRequest("GET", "/health", { auth: false, timeoutMs: opts.timeoutMs, acceptAnyStatus: true });
    return { reachable: status > 0, status, body };
  }

  async listDatasets(opts: RequestOptions = {}): Promise<DatasetDTO[]> {
    const body = await this.request<unknown>("GET", "/api/v1/datasets", { ...opts, retries: opts.retries ?? 1 });
    return Array.isArray(body) ? (body.filter((row) => row && typeof row === "object") as DatasetDTO[]) : [];
  }

  /** POST /api/v1/datasets — idempotent create-or-return by name. */
  async ensureDataset(name: string, opts: RequestOptions = {}): Promise<DatasetDTO> {
    const body = await this.request<DatasetDTO>("POST", "/api/v1/datasets", { ...opts, json: { name } });
    if (!body || typeof body.id !== "string") {
      throw new CogneeApiError("dataset create returned no id", { kind: "malformed", method: "POST", path: "/api/v1/datasets" });
    }
    return body;
  }

  // -- writes ---------------------------------------------------------------

  /**
   * POST /api/v1/remember with `run_in_background=true`: the server answers as
   * soon as the upload is stored and builds the graph afterwards. The response
   * carries no document id; the document is found later by its upload name in
   * the dataset listing. A conflict with an existing upload name (HTTP 409 on
   * a synchronous write) is *not* surfaced on the background path — another
   * reason the filename is derived from the content.
   */
  async remember(input: RememberRequest, opts: RequestOptions = {}): Promise<RememberResponse> {
    const form = new FormData();
    form.append("data", new Blob([input.text], { type: "text/plain" }), input.filename);
    form.append("datasetName", input.datasetName);
    form.append("run_in_background", "true");
    const body = await this.request<RememberResponse>("POST", "/api/v1/remember", { ...opts, body: form, retries: 0 });
    return body && typeof body === "object" ? body : {};
  }

  /** POST /api/v1/forget with datasetId + dataId: exactly one document, by construction. */
  async forgetDocument(datasetId: string, dataId: string, opts: RequestOptions = {}): Promise<Record<string, unknown>> {
    const body = await this.request<Record<string, unknown>>("POST", "/api/v1/forget", {
      ...opts,
      json: { datasetId: requireUuid(datasetId, "dataset id"), dataId: requireUuid(dataId, "data id") },
      retries: 0,
    });
    return body && typeof body === "object" ? body : {};
  }

  // -- reads ----------------------------------------------------------------

  async recall(input: RecallRequest, opts: RequestOptions = {}): Promise<RecallItem[]> {
    if (!input.datasets.length) throw new CogneeApiError("recall needs at least one dataset", { kind: "config" });
    const payload: Record<string, unknown> = {
      query: input.query,
      datasets: input.datasets,
      // Graph only, always stated: an omitted scope resolves to "auto".
      scope: ["graph"],
      search_type: input.searchType,
      top_k: input.topK,
    };
    if (input.includeReferences) payload.include_references = true;
    const body = await this.request<unknown>("POST", "/api/v1/recall", { ...opts, json: payload, retries: opts.retries ?? 1 });
    if (Array.isArray(body)) return body.filter((row) => row && typeof row === "object") as RecallItem[];
    if (body && typeof body === "object") return [body as RecallItem];
    return [];
  }

  /** The cognify pipeline status for one dataset, uppercased; "" when unknown. */
  async datasetStatus(datasetId: string, opts: RequestOptions = {}): Promise<string> {
    const id = requireUuid(datasetId, "dataset id");
    const query = new URLSearchParams({ dataset: id, pipeline: "cognify_pipeline" });
    const body = await this.request<unknown>("GET", `/api/v1/datasets/status?${query.toString()}`, { ...opts, retries: opts.retries ?? 1 });
    if (!body || typeof body !== "object") return "";
    const record = body as Record<string, unknown>;
    let value: unknown = record[id];
    if (value === undefined && Object.keys(record).length === 1) value = Object.values(record)[0];
    if (value && typeof value === "object") value = (value as Record<string, unknown>)["cognify_pipeline"];
    return typeof value === "string" ? value.toUpperCase() : "";
  }

  async listDatasetData(datasetId: string, page: { limit?: number; offset?: number } = {}, opts: RequestOptions = {}): Promise<DataItemDTO[]> {
    const id = requireUuid(datasetId, "dataset id");
    const query = new URLSearchParams();
    if (page.limit !== undefined) query.set("limit", String(page.limit));
    if (page.offset !== undefined) query.set("offset", String(page.offset));
    const suffix = query.size ? `?${query.toString()}` : "";
    const body = await this.request<unknown>("GET", `/api/v1/datasets/${id}/data${suffix}`, { ...opts, retries: opts.retries ?? 1 });
    return Array.isArray(body) ? (body.filter((row) => row && typeof row === "object") as DataItemDTO[]) : [];
  }

  /** Walk the dataset listing until `name` is found or `maxPages` is exhausted. */
  async findDataItemByName(datasetId: string, name: string, opts: RequestOptions & { maxPages?: number } = {}): Promise<DataItemDTO | undefined> {
    const limit = 100;
    const maxPages = opts.maxPages ?? 20;
    for (let page = 0; page < maxPages; page += 1) {
      const rows = await this.listDatasetData(datasetId, { limit, offset: page * limit }, opts);
      const hit = rows.find((row) => row.name === name);
      if (hit) return hit;
      if (rows.length < limit) return undefined;
    }
    return undefined;
  }

  // -- transport ------------------------------------------------------------

  private async request<T>(
    method: string,
    path: string,
    opts: RequestOptions & { json?: unknown; body?: BodyInit },
  ): Promise<T> {
    const retries = Math.max(0, opts.retries ?? 0);
    const deadline = Date.now() + (opts.timeoutMs ?? this.requestTimeoutMs);
    let attempt = 0;
    for (;;) {
      const remaining = deadline - Date.now();
      if (attempt > 0 && remaining <= 0) {
        throw new CogneeApiError(`${method} ${path} timed out after retries`, { kind: "timeout", method, path });
      }
      try {
        const { status, body } = await this.rawRequest(method, path, {
          auth: true,
          json: opts.json,
          body: opts.body,
          timeoutMs: attempt === 0 ? (opts.timeoutMs ?? this.requestTimeoutMs) : remaining,
        });
        if (body && typeof body === "object" && !Array.isArray(body) && typeof (body as Record<string, unknown>).error === "string" && status < 400) {
          // Error-bearing 2xx (cognee answers some failures this way).
          throw new CogneeApiError(`${method} ${path} answered an error`, {
            kind: "http", status: 500, method, path, detail: String((body as Record<string, unknown>).error),
          });
        }
        return body as T;
      } catch (error) {
        const retryable =
          error instanceof CogneeApiError &&
          (error.kind === "network" || error.kind === "timeout" || error.isServerError || error.isRateLimited);
        if (!retryable || attempt >= retries) throw error;
        attempt += 1;
        await sleep(Math.min(RETRY_BASE_DELAY_MS * 2 ** (attempt - 1), Math.max(0, deadline - Date.now())));
      }
    }
  }

  private async rawRequest(
    method: string,
    path: string,
    opts: { auth: boolean; json?: unknown; body?: BodyInit; timeoutMs?: number; acceptAnyStatus?: boolean },
  ): Promise<{ status: number; body: unknown }> {
    const url = `${this.baseUrl}${path}`;
    const headers: Record<string, string> = { Accept: "application/json" };
    let body: BodyInit | undefined = opts.body;
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.json);
    }
    if (opts.auth && this.apiKey) headers["X-Api-Key"] = this.apiKey;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? this.requestTimeoutMs);
    let response: Response;
    try {
      // `redirect: "manual"`: the API key must never follow a redirect to
      // another origin. A 3xx is reported as such instead.
      response = await this.fetchImpl(url, { method, headers, body, signal: controller.signal, redirect: "manual" });
    } catch (error) {
      clearTimeout(timer);
      const aborted = (error as { name?: string })?.name === "AbortError" || controller.signal.aborted;
      throw new CogneeApiError(
        aborted ? `${method} ${path} timed out` : `${method} ${path} failed: ${(error as Error)?.message ?? String(error)}`.slice(0, 300),
        { kind: aborted ? "timeout" : "network", method, path },
      );
    } finally {
      clearTimeout(timer);
    }

    if (response.status >= 300 && response.status < 400) {
      throw new CogneeApiError(`${method} ${path} redirected (HTTP ${response.status}); credentials are not forwarded across origins`, {
        kind: "redirect", status: response.status, method, path,
      });
    }

    const text = await this.readBounded(response, method, path);
    let parsed: unknown = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        if (response.ok && !opts.acceptAnyStatus) {
          throw new CogneeApiError(`${method} ${path} returned malformed JSON`, { kind: "malformed", method, path, detail: text.slice(0, 120) });
        }
        parsed = text;
      }
    }
    if (!response.ok && !opts.acceptAnyStatus) {
      throw new CogneeApiError(`${method} ${path} failed (HTTP ${response.status})`, {
        kind: "http", status: response.status, method, path, detail: extractDetail(parsed, text),
      });
    }
    return { status: response.status, body: parsed };
  }

  private async readBounded(response: Response, method: string, path: string): Promise<string> {
    const declared = Number(response.headers.get("content-length") ?? "0");
    if (declared > this.maxResponseBytes) {
      throw new CogneeApiError(`${method} ${path} response too large (${declared} bytes)`, { kind: "malformed", method, path });
    }
    if (!response.body) return "";
    const reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let total = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) {
        total += value.byteLength;
        if (total > this.maxResponseBytes) {
          await reader.cancel().catch(() => undefined);
          throw new CogneeApiError(`${method} ${path} response too large (> ${this.maxResponseBytes} bytes)`, { kind: "malformed", method, path });
        }
        chunks.push(value);
      }
    }
    return Buffer.concat(chunks.map((chunk) => Buffer.from(chunk))).toString("utf8");
  }
}

function extractDetail(parsed: unknown, text: string): string {
  if (parsed && typeof parsed === "object") {
    const record = parsed as Record<string, unknown>;
    for (const key of ["detail", "error", "message"]) {
      const value = record[key];
      if (typeof value === "string") return value;
      if (value && typeof value === "object") return JSON.stringify(value).slice(0, 300);
    }
  }
  return text.slice(0, 300);
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function requireUuid(value: string, label: string): string {
  const trimmed = String(value ?? "").trim();
  if (!UUID.test(trimmed)) throw new CogneeApiError(`${label} is not a UUID`, { kind: "config" });
  return trimmed;
}
