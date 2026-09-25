/**
 * `CogneeClient` is the only place in this package that talks to the
 * network. Retry/auth semantics follow @cognee/cognee-openclaw's client,
 * except this one also retries 5xx/429/network failures, not just timeouts
 * (see `errors.ts#isRetryable`). Takes an already-resolved
 * `CogneeMastraConfig`; never reads `process.env` itself.
 */

import {
  getCapabilities,
  isRouteMissing,
  resolveAuthPrefix,
  resolveRecallPath,
  type AuthPrefix,
  type RecallPath,
} from "./capabilities.js";
import { CogneeApiError, isRetryable } from "./errors.js";
import type {
  AddRequest,
  CogneeMastraConfig,
  CognifyRequest,
  DatasetDTO,
  DatasetStatusResponse,
  ForgetRequest,
  HealthResponse,
  ImproveRequest,
  LoginResponse,
  QAEntryDTO,
  RecallHit,
  RecallRequest,
  RecallResponse,
  RecallResponseItem,
  RememberEntryRequest,
  RememberRequest,
  RememberResult,
  SearchResponse,
  SearchResponseItem,
} from "./types.js";

// -- Constants --------------------------------------------------------------

const DEFAULT_BASE_URL = "http://localhost:8000";
const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
const DEFAULT_RETRIES = 3;
/** Exponential backoff base, in ms. Attempt N's delay (N >= 1) is
 *  `RETRY_BASE_DELAY_MS * 2 ** (N - 1)`: 3s, 6s, 12s, ... */
const RETRY_BASE_DELAY_MS = 3_000;
/** Login gets its own timeout/retry budget instead of inheriting
 *  requestTimeoutMs × (retries+1) — else a 10s-bounded tool call could sit
 *  ~141s in login on an unresponsive server before its own bound applied. */
const LOGIN_TIMEOUT_MS = 10_000;
const LOGIN_RETRIES = 1;

/** Per-call override for any request method. Setting `timeoutMs` alone
 *  disables retries for that call (the recall path relies on this); set
 *  `retries` explicitly to keep some retries within a bounded budget. */
export interface RequestOptions {
  timeoutMs?: number;
  retries?: number;
}

// -- CogneeClient -------------------------------------------------------------

export class CogneeClient {
  readonly baseUrl: string;

  private apiKey: string | undefined;
  private readonly email: string | undefined;
  private readonly password: string | undefined;
  private readonly requestTimeoutMs: number;
  private readonly retries: number;
  private readonly debug: boolean;
  private readonly fetchImpl: typeof fetch;

  private authToken: string | undefined;
  private loginPromise: Promise<void> | undefined;

  /** ensureDataset()'s cache, keyed by name; datasetInFlight collapses
   *  concurrent calls for the same name onto one request. */
  private readonly datasetCache = new Map<string, DatasetDTO>();
  private readonly datasetInFlight = new Map<string, Promise<DatasetDTO>>();

  constructor(config: CogneeMastraConfig = {}) {
    this.baseUrl = stripTrailingSlash(config.baseUrl?.trim() || DEFAULT_BASE_URL);
    // apiKey wins over auth: enforced by never reading auth once apiKey is
    // set (ensureAuth()/login()), not by clearing fields here.
    this.apiKey = config.apiKey?.trim() || undefined;
    this.email = config.auth?.email;
    this.password = config.auth?.password;
    this.requestTimeoutMs = config.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
    this.retries = config.retries ?? DEFAULT_RETRIES;
    this.debug = config.debug ?? false;
    this.fetchImpl = config.fetch ?? fetch;
  }

  /** Inject/replace the API key post-construction; from then on X-Api-Key
   *  is used and the JWT fallback is never consulted again. */
  setApiKey(key: string): void {
    const trimmed = key.trim();
    if (trimmed) this.apiKey = trimmed;
  }

  // -- Auth -------------------------------------------------------------

  private buildAuthHeaders(): Record<string, string> {
    if (this.apiKey) {
      // Never also send a stale Bearer token: a server validating
      // Authorization as JWT could reject on a bogus Bearer first.
      return { "X-Api-Key": this.apiKey };
    }
    if (this.authToken) {
      return { Authorization: `Bearer ${this.authToken}` };
    }
    return {};
  }

  private async ensureAuth(): Promise<void> {
    if (this.apiKey || this.authToken) return;
    if (!this.email || !this.password) {
      // status 0 keeps this out of isRetryable's 5xx/429 matrix so retry
      // never spins on a config problem retrying can't fix.
      throw new CogneeApiError(
        "cognee client has no credentials configured — set apiKey, or auth.email + auth.password",
        { status: 0 },
      );
    }
    if (!this.loginPromise) {
      this.loginPromise = this.login().catch((error: unknown) => {
        this.loginPromise = undefined;
        throw error;
      });
    }
    return this.loginPromise;
  }

  /** POST {authPrefix}/auth/login, form-encoded (fastapi-users'
   *  OAuth2PasswordRequestForm — username/password field names).
   *  Callable directly to force re-authentication; always overwrites the
   *  cached token. */
  async login(): Promise<void> {
    if (!this.email || !this.password) {
      throw new CogneeApiError(
        "cognee client has no credentials configured — set apiKey, or auth.email + auth.password",
        { status: 0 },
      );
    }
    const cache = getCapabilities(this.baseUrl);
    const token = await resolveAuthPrefix(cache, (prefix) => this.performLogin(prefix));
    this.authToken = token;
  }

  private async performLogin(prefix: AuthPrefix): Promise<string> {
    const url = `${this.baseUrl}${prefix}/login`;
    const body = new URLSearchParams({ username: this.email ?? "", password: this.password ?? "" }).toString();
    const { data } = await this.request<LoginResponse>(
      `${prefix}/login`,
      { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body },
      { skipAuth: true, timeoutMs: Math.min(LOGIN_TIMEOUT_MS, this.requestTimeoutMs), retries: LOGIN_RETRIES },
    );
    if (!data || typeof data.access_token !== "string" || !data.access_token) {
      throw new CogneeApiError("cognee login succeeded but the response had no access_token", {
        status: 0,
        url,
        method: "POST",
      });
    }
    return data.access_token;
  }

  // -- Health -------------------------------------------------------------

  /** GET /health — no /api/v1 prefix, no auth; parses and returns the body
   *  on any status (even 503) instead of throwing, since cognee's health
   *  router keeps status/health/reason meaningful on failure too. */
  async health(): Promise<HealthResponse> {
    const url = `${this.baseUrl}/health`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.requestTimeoutMs);
    const startedAt = Date.now();
    try {
      const response = await this.fetchImpl(url, { method: "GET", signal: controller.signal });
      const body = await this.parseBody(response);
      this.debugLog("GET", url, response.status, Date.now() - startedAt, 0);
      return (body && typeof body === "object" ? body : {}) as HealthResponse;
    } catch (error) {
      throw this.normalizeTransportError(error, url, "GET", this.requestTimeoutMs);
    } finally {
      clearTimeout(timer);
    }
  }

  // -- Datasets -------------------------------------------------------------

  /** POST /api/v1/datasets — idempotent create-or-return by name; safe to
   *  call on every write since cognee returns the existing dataset unchanged. */
  async ensureDataset(name: string, opts: RequestOptions = {}): Promise<DatasetDTO> {
    const cached = this.datasetCache.get(name);
    if (cached) return cached;
    let pending = this.datasetInFlight.get(name);
    if (!pending) {
      pending = this.createDataset(name, opts)
        .then((dto) => {
          this.datasetCache.set(name, dto);
          return dto;
        })
        .finally(() => {
          this.datasetInFlight.delete(name);
        });
      this.datasetInFlight.set(name, pending);
    }
    return pending;
  }

  private async createDataset(name: string, opts: RequestOptions): Promise<DatasetDTO> {
    const { data } = await this.request<DatasetDTO>(
      "/api/v1/datasets",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) },
      { timeoutMs: opts.timeoutMs, retries: opts.retries },
    );
    return data;
  }

  /** GET /api/v1/datasets — every dataset the authenticated user can read. */
  async listDatasets(opts: RequestOptions = {}): Promise<DatasetDTO[]> {
    const { data } = await this.request<DatasetDTO[]>(
      "/api/v1/datasets",
      { method: "GET" },
      { timeoutMs: opts.timeoutMs, retries: opts.retries },
    );
    return Array.isArray(data) ? data : [];
  }

  /** GET /api/v1/datasets/status; pipeline values are add_pipeline |
   *  cognify_pipeline | code_graph_pipeline, hence the default below. */
  async datasetStatus(
    datasetId: string,
    pipeline: string = "cognify_pipeline",
    opts: RequestOptions = {},
  ): Promise<DatasetStatusResponse> {
    const query = new URLSearchParams();
    query.append("dataset", datasetId);
    query.append("pipeline", pipeline);
    const { data } = await this.request<DatasetStatusResponse>(
      `/api/v1/datasets/status?${query.toString()}`,
      { method: "GET" },
      { timeoutMs: opts.timeoutMs, retries: opts.retries },
    );
    return data;
  }

  // -- Writes: remember / add / cognify -------------------------------------

  /** POST /api/v1/remember — primary write path; cognee's
   *  get_remember_router.py combines add+cognify (and bridges session_id's
   *  session cache into the graph) behind one route, so a successful call
   *  never needs an explicit cognify(). Falls back once to add()+cognify()
   *  on a 404, cached per base URL via capabilities.ts. */
  async remember(input: RememberRequest, opts: RequestOptions = {}): Promise<RememberResult> {
    const cache = getCapabilities(this.baseUrl);
    if (cache.rememberSupported === false) {
      return this.rememberViaAddCognify(input, opts);
    }
    try {
      const result = await this.rememberDirect(input, opts);
      cache.rememberSupported = true;
      return result;
    } catch (error) {
      if (cache.rememberSupported === undefined && isRouteMissing(error)) {
        const result = await this.rememberViaAddCognify(input, opts);
        cache.rememberSupported = false;
        return result;
      }
      throw error;
    }
  }

  private async rememberDirect(input: RememberRequest, opts: RequestOptions): Promise<RememberResult> {
    const { data } = await this.request<RememberResult>(
      "/api/v1/remember",
      { method: "POST", body: this.buildRememberForm(input) },
      { timeoutMs: opts.timeoutMs, retries: opts.retries },
    );
    return data;
  }

  // Fallback leg for remember(): same write split into add()+cognify(),
  // minus session_id — get_add_router.py declares no such form field.
  private async rememberViaAddCognify(input: RememberRequest, opts: RequestOptions): Promise<RememberResult> {
    const addResult = await this.add(
      {
        raw_data: input.raw_data,
        datasetName: input.datasetName,
        datasetId: input.datasetId,
        node_set: input.node_set,
        run_in_background: input.run_in_background,
      },
      opts,
    );
    const datasetId = typeof addResult["dataset_id"] === "string" ? (addResult["dataset_id"] as string) : input.datasetId;
    const datasetNames = !datasetId && input.datasetName ? [input.datasetName] : undefined;
    await this.cognify(datasetId ? [datasetId] : [], { ...opts, datasetNames });
    return addResult;
  }

  /** POST /api/v1/add — fallback leg 1 when `/remember` is absent. */
  async add(input: AddRequest, opts: RequestOptions = {}): Promise<RememberResult> {
    const { data } = await this.request<RememberResult>(
      "/api/v1/add",
      { method: "POST", body: this.buildAddForm(input) },
      { timeoutMs: opts.timeoutMs, retries: opts.retries },
    );
    return data;
  }

  /** POST /api/v1/cognify — fallback leg 2, paired with `add()`. */
  async cognify(
    datasetIds: string[] = [],
    opts: RequestOptions & { datasetNames?: string[] } = {},
  ): Promise<RememberResult> {
    const body: CognifyRequest = {
      run_in_background: true,
      ...(datasetIds.length ? { dataset_ids: datasetIds } : {}),
      ...(opts.datasetNames && opts.datasetNames.length ? { datasets: opts.datasetNames } : {}),
    };
    const { data } = await this.request<RememberResult>(
      "/api/v1/cognify",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      { timeoutMs: opts.timeoutMs, retries: opts.retries },
    );
    return data;
  }

  /** POST /api/v1/remember/entry; the 404 fallback below exists
   *  defensively for older servers that mount it differently. */
  async rememberEntry(input: RememberEntryRequest, opts: RequestOptions = {}): Promise<RememberResult> {
    const cache = getCapabilities(this.baseUrl);
    if (cache.rememberEntrySupported === false) {
      return this.rememberEntryViaRemember(input, opts);
    }
    try {
      const result = await this.rememberEntryDirect(input, opts);
      cache.rememberEntrySupported = true;
      return result;
    } catch (error) {
      if (cache.rememberEntrySupported === undefined && isRouteMissing(error)) {
        const result = await this.rememberEntryViaRemember(input, opts);
        cache.rememberEntrySupported = false;
        return result;
      }
      throw error;
    }
  }

  private async rememberEntryDirect(input: RememberEntryRequest, opts: RequestOptions): Promise<RememberResult> {
    const { data } = await this.request<RememberResult>(
      "/api/v1/remember/entry",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) },
      { timeoutMs: opts.timeoutMs, retries: opts.retries },
    );
    return data;
  }

  // Fallback leg: degrades to remember() with the turn serialized as
  // raw_data — loses typed session-cache indexing but stays recallable.
  private async rememberEntryViaRemember(input: RememberEntryRequest, opts: RequestOptions): Promise<RememberResult> {
    return this.remember(
      {
        raw_data: [formatQAEntryAsText(input.entry)],
        datasetName: input.dataset_name,
        datasetId: input.dataset_id,
        session_id: input.session_id,
      },
      opts,
    );
  }

  private buildRememberForm(input: RememberRequest): FormData {
    const form = new FormData();
    for (const item of input.raw_data) form.append("raw_data", item);
    if (input.datasetName) form.append("datasetName", input.datasetName);
    if (input.datasetId) form.append("datasetId", input.datasetId);
    if (input.session_id) form.append("session_id", input.session_id);
    if (input.node_set) for (const tag of input.node_set) form.append("node_set", tag);
    if (typeof input.run_in_background === "boolean") {
      form.append("run_in_background", String(input.run_in_background));
    }
    return form;
  }

  private buildAddForm(input: AddRequest): FormData {
    const form = new FormData();
    for (const item of input.raw_data) form.append("raw_data", item);
    if (input.datasetName) form.append("datasetName", input.datasetName);
    if (input.datasetId) form.append("datasetId", input.datasetId);
    if (input.node_set) for (const tag of input.node_set) form.append("node_set", tag);
    if (typeof input.run_in_background === "boolean") {
      form.append("run_in_background", String(input.run_in_background));
    }
    return form;
  }

  // -- Reads: recall ---------------------------------------------------------

  /** POST /api/v1/recall, falling back once to the legacy POST
   *  /api/v1/search on a 404. Both shapes normalize into RecallHit[] here,
   *  not in the shared probe helper — see resolveRecallPath's doc. */
  async recall(input: RecallRequest, opts: RequestOptions = {}): Promise<RecallHit[]> {
    const cache = getCapabilities(this.baseUrl);
    return resolveRecallPath(cache, (path) => this.performRecall(path, input, opts));
  }

  private async performRecall(path: RecallPath, input: RecallRequest, opts: RequestOptions): Promise<RecallHit[]> {
    const { data } = await this.request<unknown>(
      path,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) },
      { timeoutMs: opts.timeoutMs, retries: opts.retries },
    );
    return path === "/api/v1/recall"
      ? normalizeRecallResponse(data as RecallResponse)
      : normalizeSearchResponse(data as SearchResponse);
  }

  // -- Forget -----------------------------------------------------------------

  /** POST /api/v1/forget. everything: true is hard-blocked here too, not
   *  only in tools.ts's cognee_forget — CogneeClient is itself an escape
   *  hatch any direct caller could reach. */
  async forget(input: ForgetRequest, opts: RequestOptions = {}): Promise<RememberResult> {
    if (input.everything) {
      throw new CogneeApiError(
        "cognee client refuses forget({ everything: true }) — this permanently deletes every dataset the authenticated user owns. Use the cognee server/CLI directly if that is really intended.",
        { status: 0 },
      );
    }
    const { data } = await this.request<RememberResult>(
      "/api/v1/forget",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) },
      { timeoutMs: opts.timeoutMs, retries: opts.retries },
    );
    return data;
  }

  // -- Improve (optional) -----------------------------------------------

  /** POST /api/v1/improve — off by default (`improveOnThreadEnd: false`);
   *  exposed here for a caller who wants to trigger it manually. */
  async improve(input: ImproveRequest, opts: RequestOptions = {}): Promise<unknown> {
    const { data } = await this.request<unknown>(
      "/api/v1/improve",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) },
      { timeoutMs: opts.timeoutMs, retries: opts.retries },
    );
    return data;
  }

  // -- Shared HTTP transport: auth + retry + timeout + error wrapping --------

  /** Every method above calls through here: attach auth headers, run under
   *  an AbortSignal timeout, retry with RETRY_BASE_DELAY_MS exponential
   *  backoff, and re-login once on a 401 (JWT mode only) before retrying.
   *
   *  maxRetries is 0 whenever the caller passed a per-call timeoutMs (the
   *  recall path never retries), else this.retries; an explicit
   *  options.retries overrides both, letting a bounded-budget write keep
   *  one retry within its own budget. Retryable failures are
   *  errors.ts#isRetryable (5xx, 429) plus status 0 network/timeout
   *  failures, which isRetryable has no opinion on. */
  private async request<T>(
    path: string,
    init: { method: string; headers?: Record<string, string>; body?: RequestInit["body"] },
    options: { timeoutMs?: number; retries?: number; skipAuth?: boolean } = {},
  ): Promise<{ status: number; data: T }> {
    if (!options.skipAuth) await this.ensureAuth();

    const url = `${this.baseUrl}${path}`;
    const perCallTimeout = options.timeoutMs;
    const timeoutMs = perCallTimeout ?? this.requestTimeoutMs;
    const maxRetries = options.retries !== undefined ? options.retries : perCallTimeout !== undefined ? 0 : this.retries;

    let lastError: unknown;
    for (let attempt = 0; attempt <= maxRetries; attempt++) {
      if (attempt > 0) {
        await sleep(RETRY_BASE_DELAY_MS * 2 ** (attempt - 1));
      }

      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      const startedAt = Date.now();
      try {
        const headers = options.skipAuth
          ? { ...init.headers }
          : { ...this.buildAuthHeaders(), ...init.headers };
        const response = await this.fetchImpl(url, {
          method: init.method,
          headers,
          body: init.body,
          signal: controller.signal,
        });

        // 401 in JWT mode only (a bad API key isn't a stale token): drop
        // the cached token and retry once, independent of maxRetries.
        if (response.status === 401 && !options.skipAuth && !this.apiKey) {
          clearTimeout(timer);
          this.authToken = undefined;
          this.loginPromise = undefined;
          await this.ensureAuth();

          const retryController = new AbortController();
          const retryTimer = setTimeout(() => retryController.abort(), timeoutMs);
          try {
            const retryHeaders = { ...this.buildAuthHeaders(), ...init.headers };
            const retryResponse = await this.fetchImpl(url, {
              method: init.method,
              headers: retryHeaders,
              body: init.body,
              signal: retryController.signal,
            });
            const result = await this.finish<T>(retryResponse, url, init.method);
            this.debugLog(init.method, url, retryResponse.status, Date.now() - startedAt, attempt);
            return result;
          } finally {
            clearTimeout(retryTimer);
          }
        }

        const result = await this.finish<T>(response, url, init.method);
        clearTimeout(timer);
        this.debugLog(init.method, url, response.status, Date.now() - startedAt, attempt);
        return result;
      } catch (error) {
        clearTimeout(timer);
        const normalized = this.normalizeTransportError(error, url, init.method, timeoutMs);
        lastError = normalized;
        this.debugLog(init.method, url, normalized.status, Date.now() - startedAt, attempt, normalized.message);
        const retryable = normalized.status === 0 || isRetryable(normalized.status);
        if (retryable && attempt < maxRetries) continue;
        throw normalized;
      }
    }
    // Unreachable: the loop above always returns or throws; kept for type safety.
    throw lastError ?? new CogneeApiError("cognee request failed for an unknown reason", { status: 0, url, method: init.method });
  }

  // Where a raw Response becomes either data or a typed CogneeApiError
  // (with the parsed body attached) for every caller in this file.
  private async finish<T>(response: Response, url: string, method: string): Promise<{ status: number; data: T }> {
    const body = await this.parseBody(response);
    if (!response.ok) {
      throw CogneeApiError.fromResponse(response.status, body, { url, method });
    }
    return { status: response.status, data: body as T };
  }

  // Shape-agnostic: most responses are JSON, some error paths (and
  // /health's failure body) are plain text, and an empty body is undefined.
  private async parseBody(response: Response): Promise<unknown> {
    const text = await response.text();
    if (!text) return undefined;
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }

  // Normalizes any thrown value into a CogneeApiError with status always
  // populated: a CogneeApiError passes through; an AbortError (our own
  // timeout, no caller AbortSignal exists here) becomes fromTimeout;
  // anything else becomes fromNetworkError. Both use status 0 — no
  // response was ever received.
  private normalizeTransportError(error: unknown, url: string, method: string, timeoutMs: number): CogneeApiError {
    if (error instanceof CogneeApiError) return error;
    const isAbort = error instanceof DOMException || (error instanceof Error && error.name === "AbortError");
    if (isAbort) return CogneeApiError.fromTimeout(timeoutMs, { url, method });
    return CogneeApiError.fromNetworkError(error, { url, method });
  }

  // Gated by config.debug; logs method/path/status/timing only, never
  // headers or a body, so login's password/token can never reach a log line.
  private debugLog(
    method: string,
    url: string,
    status: number,
    durationMs: number,
    attempt: number,
    note?: string,
  ): void {
    if (!this.debug) return;
    const safeUrl = url.split("?")[0];
    const suffix = note ? ` (${note})` : "";
    // eslint-disable-next-line no-console -- intentional, gated debug output
    console.debug(
      `[cognee-mastra] ${method} ${safeUrl} -> ${status} (${durationMs}ms, attempt ${attempt + 1})${suffix}`,
    );
  }
}

// -- Helpers (module-private) -------------------------------------------------

function stripTrailingSlash(url: string): string {
  return url.endsWith("/") ? url.slice(0, -1) : url;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Renders a QAEntryDTO as plain text for the rememberEntry() -> remember() fallback leg.
function formatQAEntryAsText(entry: QAEntryDTO): string {
  const parts = [`Q: ${entry.question}`, `A: ${entry.answer}`];
  if (entry.context) parts.push(`Context: ${entry.context}`);
  return parts.join("\n");
}

function safeStringify(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function normalizeRecallResponse(items: unknown): RecallHit[] {
  if (!Array.isArray(items)) return [];
  return items.map(normalizeRecallResponseItem);
}

/** Best-effort readable text for a /recall item with no `text`: on cognee
 *  1.5.4 every observed item already has one, so this covers the session-
 *  cache shapes (question/answer/context, or a bare content) so a
 *  pre-bridge session hit reads as prose, not a JSON dump. */
function composeRecallText(record: Record<string, unknown>): string {
  const str = (v: unknown): string | undefined => (typeof v === "string" && v.trim() ? v : undefined);
  const content = str(record["content"]);
  if (content) return content;
  const question = str(record["question"]);
  const answer = str(record["answer"]);
  const context = str(record["context"]);
  if (question || answer || context) {
    return [question && `Question: ${question}`, answer && `Answer: ${answer}`, context && `Context: ${context}`]
      .filter(Boolean)
      .join("\n");
  }
  return safeStringify(record);
}

function normalizeRecallResponseItem(item: unknown): RecallHit {
  if (!item || typeof item !== "object") {
    return { text: safeStringify(item) };
  }
  const record = item as RecallResponseItem;
  return {
    text: typeof record.text === "string" ? record.text : composeRecallText(record),
    score: record.score ?? null,
    datasetId: record.dataset_id ?? null,
    datasetName: record.dataset_name ?? null,
    source: typeof record.source === "string" ? record.source : undefined,
    metadata: record.metadata,
  };
}

// Different parse from normalizeRecallResponseItem on purpose: /search
// wraps a free-form search_result (SearchResponseItem), not /recall's envelope.
function normalizeSearchResponse(items: unknown): RecallHit[] {
  if (!Array.isArray(items)) return [];
  return items.map(normalizeSearchResponseItem);
}

function normalizeSearchResponseItem(item: unknown): RecallHit {
  if (!item || typeof item !== "object") {
    return { text: safeStringify(item) };
  }
  const record = item as SearchResponseItem;
  const raw = record.search_result;
  const text = typeof raw === "string" ? raw : Array.isArray(raw) ? raw.map(String).join("\n") : safeStringify(raw);
  return {
    text,
    datasetId: record.dataset_id ?? null,
    datasetName: record.dataset_name ?? null,
  };
}
