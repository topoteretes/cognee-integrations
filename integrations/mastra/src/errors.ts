/**
 * `CogneeApiError` — the one error type `client.ts` throws for any
 * non-2xx cognee response, plus `isRetryable(status)`, the single source of
 * truth for the client's retry loop and the 402/403/404/409/422/5xx matrix.
 *
 * The server's error envelope isn't uniform: most failures come back as
 * `{detail: "..."}`, but some routes use `{error: "..."}` instead (e.g.
 * `get_recall_router.py`'s 422 branch), and `/health` uses a bare `message`
 * key — `extractMessage` below checks all three.
 */

/** Context captured alongside the HTTP status for debugging/logging. */
export interface CogneeApiErrorOptions {
  status: number;
  /**
   * Parsed JSON body when the response was JSON, raw text otherwise, undefined if the body could
   * not be read at all.
   */
  body?: unknown;
  url?: string;
  method?: string;
  cause?: unknown;
}

/**
 * Thrown for any cognee response outside 2xx, and for network/timeout failures with no response
 * (status `0` — callers check `status > 0` to tell the two apart).
 */
export class CogneeApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  readonly url?: string;
  readonly method?: string;

  constructor(message: string, options: CogneeApiErrorOptions) {
    super(message, options.cause !== undefined ? { cause: options.cause } : undefined);
    this.name = "CogneeApiError";
    this.status = options.status;
    this.body = options.body;
    this.url = options.url;
    this.method = options.method;

    // Keeps `instanceof CogneeApiError` correct even under a downstream re-transpile that breaks
    // the Error prototype chain.
    Object.setPrototypeOf(this, CogneeApiError.prototype);
  }

  /**
   * Build a `CogneeApiError` from a fetch Response's status + parsed body, deriving a
   * human-readable message from whichever error-envelope key the server used.
   */
  static fromResponse(
    status: number,
    body: unknown,
    context: { url?: string; method?: string } = {},
  ): CogneeApiError {
    const message = extractMessage(body) ?? `cognee API responded with status ${status}`;
    return new CogneeApiError(message, { status, body, ...context });
  }

  /**
   * Build a `CogneeApiError` for a request that never got a response (network error, DNS failure,
   * connection refused). `status` is 0 — never a real HTTP status — so `isRetryable`/`status > 0`
   * checks can tell this apart from a real 4xx/5xx.
   */
  static fromNetworkError(
    cause: unknown,
    context: { url?: string; method?: string } = {},
  ): CogneeApiError {
    const message = cause instanceof Error ? cause.message : String(cause);
    return new CogneeApiError(`cognee request failed: ${message}`, { status: 0, cause, ...context });
  }

  /**
   * Build a `CogneeApiError` for a request aborted by an `AbortSignal` timeout. `status` is 0,
   * matching `fromNetworkError` — a timeout never received a response either.
   */
  static fromTimeout(timeoutMs: number, context: { url?: string; method?: string } = {}): CogneeApiError {
    return new CogneeApiError(`cognee request timed out after ${timeoutMs}ms`, { status: 0, ...context });
  }
}

function extractMessage(body: unknown): string | undefined {
  if (!body || typeof body !== "object") {
    return typeof body === "string" && body.trim() ? body : undefined;
  }
  const record = body as Record<string, unknown>;
  for (const key of ["detail", "error", "message"]) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}

/**
 * Whether a failed request at this status is worth retrying: `5xx`
 * (transient server failure) and `429` (rate limited) are; cognee's stable
 * "no" matrix (`402/403/404/409/422`) is not — retrying just repeats the
 * same failure at N× latency. `0` (network/timeout, no response) returns
 * `false` here too, but `client.ts`'s retry loop treats connection failures as retryable on its own
 * terms — this table simply has no opinion on status `0`.
 */
export function isRetryable(status: number): boolean {
  if (status === 429) return true;
  return status >= 500 && status < 600;
}

/**
 * Whether a failed recall at this status counts against the breaker
 * (`breaker.ts`) — narrower than `isRetryable`: `429` is worth retrying but
 * isn't evidence cognee is unhealthy. Only `0` (network/timeout) and `5xx`
 * trip it; cognee's stable "no" matrix plus `401`/`400`/`429` is a
 * deterministic response to this request (bad key, malformed query, wrong baseUrl) — breaking on it
 * would blackout recall for 120s over a config problem, not an outage.
 */
export function isBreakerError(status: number): boolean {
  return status === 0 || (status >= 500 && status < 600);
}
