/**
 * One error type for everything that goes wrong talking to cognee, so the
 * tool layer can turn it into a bounded, credential-free message.
 */
export type CogneeErrorKind = "http" | "timeout" | "network" | "malformed" | "config" | "redirect";

export class CogneeApiError extends Error {
  readonly kind: CogneeErrorKind;
  /** HTTP status for `kind === "http"`, 0 otherwise. */
  readonly status: number;
  readonly method: string;
  readonly path: string;
  /** Server-provided detail, truncated and never containing our credentials. */
  readonly detail: string;

  constructor(
    message: string,
    opts: { kind: CogneeErrorKind; status?: number; method?: string; path?: string; detail?: string },
  ) {
    super(message);
    this.name = "CogneeApiError";
    this.kind = opts.kind;
    this.status = opts.status ?? 0;
    this.method = opts.method ?? "";
    this.path = opts.path ?? "";
    this.detail = (opts.detail ?? "").slice(0, 300);
  }

  /** True when the write may have landed server-side even though we saw no 2xx. */
  get outcomeUncertain(): boolean {
    return this.kind === "timeout" || this.kind === "network";
  }

  get isAuth(): boolean {
    return this.kind === "http" && (this.status === 401 || this.status === 403);
  }

  get isNotFound(): boolean {
    return this.kind === "http" && this.status === 404;
  }

  get isRateLimited(): boolean {
    return this.kind === "http" && this.status === 429;
  }

  get isServerError(): boolean {
    return this.kind === "http" && this.status >= 500;
  }
}

export class ConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ConfigError";
  }
}

/** Shorten any error to a single safe line for tool results and logs. */
export function describeError(error: unknown): string {
  if (error instanceof CogneeApiError) {
    const where = error.method && error.path ? ` (${error.method} ${error.path})` : "";
    const status = error.status ? ` HTTP ${error.status}` : "";
    const detail = error.detail ? `: ${error.detail}` : "";
    return `${error.kind}${status}${where}${detail}`.slice(0, 400);
  }
  if (error instanceof Error) return `${error.name}: ${error.message}`.slice(0, 400);
  return String(error).slice(0, 400);
}
