/**
 * The only module that talks to cognee. Wire contract verified live against
 * cognee 1.6.5 (fixtures in `fixtures/cognee-1.6.5/`):
 *
 *   POST /api/v1/datasets                 {name} → DatasetDTO (create-or-return)
 *   GET  /api/v1/datasets                 → DatasetDTO[]
 *   POST /api/v1/remember                 multipart data=<file>, datasetName,
 *                                         run_in_background → {status, dataset_id}
 *   POST /api/v1/recall                   {query, datasets, scope:["graph"],
 *                                         search_type, top_k}
 *   GET  /api/v1/datasets/status?dataset=&pipeline=cognify_pipeline
 *   GET  /api/v1/datasets/{id}/data?limit=&offset=
 *   POST /api/v1/forget                   {datasetId, dataId}
 *   GET  /health
 *
 * Requests go through the `fetch` the host hands the plugin (`ctx.http.fetch`,
 * host-governed and audited); auth is `X-Api-Key`, resolved per call from the
 * operator's secret reference and never cached by this class beyond the call.
 */
import { CogneeApiError } from "./errors.js";

export interface CogneeClientOptions {
  baseUrl: string;
  apiKey: string;
  fetch: (url: string, init?: RequestInit) => Promise<Response>;
  requestTimeoutMs?: number;
  maxResponseBytes?: number;
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
  [key: string]: unknown;
}

export type DataItemLookup = { status: "found"; item: DataItemDTO } | { status: "absent" } | { status: "exhausted"; pagesRead: number };

export interface DataItemListing {
  items: DataItemDTO[];
  /** False when the page cap stopped the walk before the end of the listing. */
  complete: boolean;
}

const LIST_PAGE_SIZE = 100;
/** 500 pages × 100 = 50,000 documents before a lookup reports `exhausted`. */
const DEFAULT_MAX_PAGES = 500;

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

const RETRY_BASE_DELAY_MS = 500;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function requireUuid(value: string, label: string): string {
  const trimmed = String(value ?? "").trim();
  if (!UUID.test(trimmed)) throw new CogneeApiError(`${label} is not a UUID`, { kind: "config" });
  return trimmed;
}

export class CogneeClient {
  readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly fetchImpl: CogneeClientOptions["fetch"];
  private readonly requestTimeoutMs: number;
  private readonly maxResponseBytes: number;

  constructor(options: CogneeClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.apiKey = options.apiKey;
    this.fetchImpl = options.fetch;
    this.requestTimeoutMs = options.requestTimeoutMs ?? 15_000;
    this.maxResponseBytes = options.maxResponseBytes ?? 4 * 1024 * 1024;
  }

  async health(opts: RequestOptions = {}): Promise<{ reachable: boolean; status: number }> {
    const { status } = await this.rawRequest("GET", "/health", { auth: false, timeoutMs: opts.timeoutMs, acceptAnyStatus: true });
    return { reachable: status > 0, status };
  }

  async listDatasets(opts: RequestOptions = {}): Promise<DatasetDTO[]> {
    const body = await this.request<unknown>("GET", "/api/v1/datasets", { ...opts, retries: opts.retries ?? 1 });
    return Array.isArray(body) ? (body.filter((row) => row && typeof row === "object") as DatasetDTO[]) : [];
  }

  async ensureDataset(name: string, opts: RequestOptions = {}): Promise<DatasetDTO> {
    const body = await this.request<DatasetDTO>("POST", "/api/v1/datasets", { ...opts, json: { name } });
    if (!body || typeof body.id !== "string") throw new CogneeApiError("dataset create returned no id", { kind: "malformed", method: "POST", path: "/api/v1/datasets" });
    return body;
  }

  /** Background remember: answers once the upload is stored; no document id in the response. */
  async remember(input: { datasetName: string; filename: string; text: string }, opts: RequestOptions = {}): Promise<RememberResponse> {
    const form = new FormData();
    form.append("data", new Blob([input.text], { type: "text/plain" }), input.filename);
    form.append("datasetName", input.datasetName);
    form.append("run_in_background", "true");
    const body = await this.request<RememberResponse>("POST", "/api/v1/remember", { ...opts, body: form, retries: 0 });
    return body && typeof body === "object" ? body : {};
  }

  async forgetDocument(datasetId: string, dataId: string, opts: RequestOptions = {}): Promise<Record<string, unknown>> {
    const body = await this.request<Record<string, unknown>>("POST", "/api/v1/forget", {
      ...opts,
      json: { datasetId: requireUuid(datasetId, "dataset id"), dataId: requireUuid(dataId, "data id") },
      retries: 0,
    });
    return body && typeof body === "object" ? body : {};
  }

  async recall(input: { query: string; datasets: string[]; searchType: SearchType; topK: number }, opts: RequestOptions = {}): Promise<RecallItem[]> {
    if (!input.datasets.length) throw new CogneeApiError("recall needs at least one dataset", { kind: "config" });
    const payload = { query: input.query, datasets: input.datasets, scope: ["graph"], search_type: input.searchType, top_k: input.topK };
    const body = await this.request<unknown>("POST", "/api/v1/recall", { ...opts, json: payload, retries: opts.retries ?? 1 });
    if (Array.isArray(body)) return body.filter((row) => row && typeof row === "object") as RecallItem[];
    if (body && typeof body === "object") return [body as RecallItem];
    return [];
  }

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

  /**
   * Look a document up by its upload name. `absent` means the whole listing was
   * walked and the name is not there; `exhausted` means the safety cap stopped
   * the walk first, so nothing about the document is known — callers must not
   * treat that as absence.
   */
  async findDataItemByName(datasetId: string, name: string, opts: RequestOptions & { maxPages?: number } = {}): Promise<DataItemLookup> {
    const limit = LIST_PAGE_SIZE;
    const maxPages = opts.maxPages ?? DEFAULT_MAX_PAGES;
    for (let page = 0; page < maxPages; page += 1) {
      const rows = await this.listDatasetData(datasetId, { limit, offset: page * limit }, opts);
      const hit = rows.find((row) => row.name === name);
      if (hit) return { status: "found", item: hit };
      if (rows.length < limit) return { status: "absent" };
    }
    return { status: "exhausted", pagesRead: maxPages };
  }

  /**
   * Every data item in a dataset whose name starts with `prefix` (plugin-owned
   * documents). `complete` is false when the safety cap stopped the walk before
   * a short page, i.e. the result may be missing items.
   */
  async listDataItemsByPrefix(datasetId: string, prefix: string, opts: RequestOptions & { maxPages?: number } = {}): Promise<DataItemListing> {
    const limit = LIST_PAGE_SIZE;
    const maxPages = opts.maxPages ?? DEFAULT_MAX_PAGES;
    const items: DataItemDTO[] = [];
    for (let page = 0; page < maxPages; page += 1) {
      const rows = await this.listDatasetData(datasetId, { limit, offset: page * limit }, opts);
      items.push(...rows.filter((row) => typeof row.name === "string" && row.name.startsWith(prefix)));
      if (rows.length < limit) return { items, complete: true };
    }
    return { items, complete: false };
  }

  // -- transport ------------------------------------------------------------

  private async request<T>(method: string, path: string, opts: RequestOptions & { json?: unknown; body?: BodyInit }): Promise<T> {
    const retries = Math.max(0, opts.retries ?? 0);
    const deadline = Date.now() + (opts.timeoutMs ?? this.requestTimeoutMs);
    let attempt = 0;
    for (;;) {
      const remaining = deadline - Date.now();
      if (attempt > 0 && remaining <= 0) throw new CogneeApiError(`${method} ${path} timed out after retries`, { kind: "timeout", method, path });
      try {
        const { status, body } = await this.rawRequest(method, path, {
          auth: true,
          json: opts.json,
          body: opts.body,
          timeoutMs: attempt === 0 ? (opts.timeoutMs ?? this.requestTimeoutMs) : remaining,
        });
        if (body && typeof body === "object" && !Array.isArray(body) && typeof (body as Record<string, unknown>).error === "string" && status < 400) {
          throw new CogneeApiError(`${method} ${path} answered an error`, { kind: "http", status: 500, method, path, detail: String((body as Record<string, unknown>).error) });
        }
        return body as T;
      } catch (error) {
        const retryable = error instanceof CogneeApiError && error.isTransient;
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

    // One deadline covers the whole exchange, headers and body alike: a backend or
    // proxy that answers headers and then stalls must not hold a tool call or a
    // job open indefinitely. The timer is cleared only after the body is consumed.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? this.requestTimeoutMs);
    let response: Response;
    let text: string;
    try {
      try {
        response = await this.fetchImpl(url, { method, headers, body, signal: controller.signal, redirect: "manual" });
      } catch (error) {
        const aborted = (error as { name?: string })?.name === "AbortError" || controller.signal.aborted;
        throw new CogneeApiError(
          aborted ? `${method} ${path} timed out` : `${method} ${path} failed: ${(error as Error)?.message ?? String(error)}`.slice(0, 300),
          { kind: aborted ? "timeout" : "network", method, path },
        );
      }

      if (response.status >= 300 && response.status < 400) {
        throw new CogneeApiError(`${method} ${path} redirected (HTTP ${response.status}); credentials are not forwarded across origins`, { kind: "redirect", status: response.status, method, path });
      }

      text = await this.readBounded(response, method, path, controller.signal);
    } finally {
      clearTimeout(timer);
    }
    let parsed: unknown = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        if (response.ok && !opts.acceptAnyStatus) throw new CogneeApiError(`${method} ${path} returned malformed JSON`, { kind: "malformed", method, path, detail: text.slice(0, 120) });
        parsed = text;
      }
    }
    if (!response.ok && !opts.acceptAnyStatus) {
      throw new CogneeApiError(`${method} ${path} failed (HTTP ${response.status})`, { kind: "http", status: response.status, method, path, detail: extractDetail(parsed, text) });
    }
    return { status: response.status, body: parsed };
  }

  private async readBounded(response: Response, method: string, path: string, signal: AbortSignal): Promise<string> {
    const declared = Number(response.headers.get("content-length") ?? "0");
    if (declared > this.maxResponseBytes) throw new CogneeApiError(`${method} ${path} response too large (${declared} bytes)`, { kind: "malformed", method, path });
    if (!response.body) return "";
    const reader = response.body.getReader();
    const timeout = () => new CogneeApiError(`${method} ${path} timed out while reading the response body`, { kind: "timeout", method, path });
    // A body stream does not necessarily observe the fetch signal (a stalled
    // proxy, or a test double), so the read itself races the deadline and the
    // reader is cancelled when it fires.
    let onAbort: (() => void) | undefined;
    const aborted = new Promise<never>((_, reject) => {
      onAbort = () => reject(timeout());
      if (signal.aborted) onAbort();
      else signal.addEventListener("abort", onAbort, { once: true });
    });
    aborted.catch(() => undefined);
    const chunks: Uint8Array[] = [];
    let total = 0;
    try {
      for (;;) {
        const { done, value } = await Promise.race([reader.read(), aborted]);
        if (done) break;
        if (value) {
          total += value.byteLength;
          if (total > this.maxResponseBytes) {
            throw new CogneeApiError(`${method} ${path} response too large (> ${this.maxResponseBytes} bytes)`, { kind: "malformed", method, path });
          }
          chunks.push(value);
        }
      }
    } catch (error) {
      await reader.cancel().catch(() => undefined);
      if (signal.aborted && !(error instanceof CogneeApiError && error.kind === "malformed")) throw timeout();
      throw error;
    } finally {
      if (onAbort) signal.removeEventListener("abort", onAbort);
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
