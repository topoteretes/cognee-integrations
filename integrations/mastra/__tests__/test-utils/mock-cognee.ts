/**
 * A real `node:http` server standing in for cognee, not a stubbed
 * `global.fetch`: `CogneeClient` drives real `fetch`/`AbortSignal` calls, and
 * retry/backoff timing, abort timeouts, multipart body shape, and the JWT
 * login-then-reuse flow only exist on the wire — a stub would only prove
 * "the client built some arguments." Lives in `test-utils/` rather than
 * `unit|integration|...` so Jest's `testMatch` (see `jest.config.mjs`) does
 * not try to run it as a suite.
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import {
  ADD_RESPONSE,
  COGNIFY_RESPONSE,
  datasetStatusResponse,
  ENSURE_DATASET_RESPONSE,
  FORGET_RESPONSE,
  HEALTH_RESPONSE,
  IMPROVE_RESPONSE,
  LIST_DATASETS_RESPONSE,
  LOGIN_RESPONSE,
  MOCK_API_KEY,
  MOCK_JWT_TOKEN,
  RECALL_RESPONSE,
  REMEMBER_ENTRY_RESPONSE,
  REMEMBER_RESPONSE,
  SEARCH_RESPONSE,
} from "./fixtures.js";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** One request as seen by the mock, normalized and recorded for assertions. */
export interface RecordedRequest {
  method: string;
  /** Pathname only — no query string, no origin. */
  path: string;
  query: Record<string, string>;
  /** Header names as Node lower-cases them (`x-api-key`, `authorization`, …). */
  headers: Record<string, string>;
  /** Raw body text — multipart included. Empty string for bodyless requests. */
  body: string;
  /**
   * Parsed JSON body when `content-type` was `application/json` and it parsed; otherwise
   * `undefined`.
   */
  json?: unknown;
}

/**
 * What a route handler sees. Same shape as `RecordedRequest`, named separately so a handler reading
 * a request reads distinctly from the harness recording one.
 */
export type MockRequest = RecordedRequest;

/** What a route handler returns to script the response. */
export interface MockResponse {
  status: number;
  body?: unknown;
  headers?: Record<string, string>;
}

/**
 * A per-route override. Returning a `MockResponse` sends it, after any
 * `latencyMs` delay and JSON-stringifying a non-string `body`. A handler
 * that never resolves makes the request hang indefinitely — used by e2e
 * tests to prove `budgetMs` is respected against a server that never answers.
 */
export type RouteHandler = (req: MockRequest) => MockResponse | Promise<MockResponse>;

export type AuthMode = "apiKey" | "jwt" | false;

export interface StartMockCogneeOptions {
  /**
   * Per-route overrides, keyed `"<METHOD> <path>"` with the method
   * uppercased and the path exactly as the client sends it (always
   * `/api/v1`-prefixed except `/health`), e.g. `"POST /api/v1/recall"`.
   * Overrides fully replace the built-in default, including its status code.
   */
  routes?: Record<string, RouteHandler>;
  /**
   * Delay, in ms, applied to every response (default-served or overridden) before it is written.
   * Used to test timeouts/`budgetMs`.
   */
  latencyMs?: number;
  /**
   * Fail the first N requests of any route with a 500, then fall through to
   * normal handling for the rest. Global rather than per-route: a test
   * needing per-route failure counts should register a `routes` override
   * with its own closure-scoped counter instead.
   */
  failFirstN?: number;
  /**
   * `"apiKey"`: every request except `GET /health` must carry
   * `X-Api-Key: <apiKey>` (default `MOCK_API_KEY`) or get 401. `"jwt"`:
   * every request except `GET /health` and `POST /api/v1/auth/login` must
   * carry `Authorization: Bearer <MOCK_JWT_TOKEN>` or get 401. `false`
   * (default): no auth enforcement.
   */
  requireAuth?: AuthMode;
  /**
   * Overrides the accepted API key when `requireAuth: "apiKey"`. Default `MOCK_API_KEY` from
   * `fixtures.ts`.
   */
  apiKey?: string;
}

export interface MockCognee {
  /** Base URL to hand `CogneeClient`, e.g. `http://127.0.0.1:54321`. */
  url: string;
  /**
   * Every request received, in arrival order, including ones the mock rejected for auth or forced
   * to fail.
   */
  requests: RecordedRequest[];
  close(): Promise<void>;
}

// ---------------------------------------------------------------------------
// Default route table: the happy-path response for every endpoint, from fixtures.
// ---------------------------------------------------------------------------

function defaultRoutes(): Record<string, RouteHandler> {
  return {
    "GET /health": () => ({ status: 200, body: HEALTH_RESPONSE }),
    "POST /api/v1/auth/login": () => ({ status: 200, body: LOGIN_RESPONSE }),

    "POST /api/v1/datasets": () => ({ status: 200, body: ENSURE_DATASET_RESPONSE }),
    "GET /api/v1/datasets": () => ({ status: 200, body: LIST_DATASETS_RESPONSE }),
    "GET /api/v1/datasets/status": (req) => ({
      status: 200,
      body: datasetStatusResponse(req.query.dataset),
    }),

    "POST /api/v1/remember": () => ({ status: 200, body: REMEMBER_RESPONSE }),
    "POST /api/v1/remember/entry": () => ({ status: 200, body: REMEMBER_ENTRY_RESPONSE }),
    "POST /api/v1/add": () => ({ status: 200, body: ADD_RESPONSE }),
    "POST /api/v1/cognify": () => ({ status: 200, body: COGNIFY_RESPONSE }),

    "POST /api/v1/recall": () => ({ status: 200, body: RECALL_RESPONSE }),
    "POST /api/v1/search": () => ({ status: 200, body: SEARCH_RESPONSE }),

    "POST /api/v1/forget": () => ({ status: 200, body: FORGET_RESPONSE }),
    "POST /api/v1/improve": () => ({ status: 200, body: IMPROVE_RESPONSE }),
  };
}

/**
 * Routes that are never subject to `requireAuth`, in either mode: `/health` needs no auth, and
 * login cannot require the credential it exists to issue.
 */
const AUTH_EXEMPT_ROUTES = new Set(["GET /health", "POST /api/v1/auth/login"]);

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function startMockCognee(options: StartMockCogneeOptions = {}): Promise<MockCognee> {
  const overrides = options.routes ?? {};
  const routes = defaultRoutes();
  const latencyMs = options.latencyMs ?? 0;
  const failFirstN = options.failFirstN ?? 0;
  const requireAuth: AuthMode = options.requireAuth ?? false;
  const expectedApiKey = options.apiKey ?? MOCK_API_KEY;

  const requests: RecordedRequest[] = [];
  let failuresServed = 0;

  const server: Server = createServer((req, res) => {
    void handle(req, res);
  });

  async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(chunk as Buffer);
    const body = Buffer.concat(chunks).toString("utf8");

    const parsedUrl = new URL(req.url ?? "/", "http://127.0.0.1");
    const path = parsedUrl.pathname;
    const method = (req.method ?? "GET").toUpperCase();

    let json: unknown;
    const contentType = req.headers["content-type"] ?? "";
    if (body && contentType.includes("application/json")) {
      try {
        json = JSON.parse(body);
      } catch {
        json = undefined;
      }
    }

    const headers: Record<string, string> = {};
    for (const [key, value] of Object.entries(req.headers)) {
      headers[key] = Array.isArray(value) ? value.join(",") : (value ?? "");
    }

    const recorded: RecordedRequest = {
      method,
      path,
      query: Object.fromEntries(parsedUrl.searchParams),
      headers,
      body,
      json,
    };
    requests.push(recorded);

    const routeKey = `${method} ${path}`;

    if (requireAuth && !AUTH_EXEMPT_ROUTES.has(routeKey)) {
      if (requireAuth === "apiKey") {
        if (headers["x-api-key"] !== expectedApiKey) {
          return send(res, 401, { detail: "missing or invalid X-Api-Key" }, latencyMs);
        }
      } else if (requireAuth === "jwt") {
        const expected = `Bearer ${MOCK_JWT_TOKEN}`;
        if (headers["authorization"] !== expected) {
          return send(res, 401, { detail: "missing or invalid bearer token" }, latencyMs);
        }
      }
    }

    if (failuresServed < failFirstN) {
      failuresServed += 1;
      return send(res, 500, { detail: "mock: scripted failure" }, latencyMs);
    }

    const handler = overrides[routeKey] ?? routes[routeKey];
    if (!handler) {
      return send(res, 404, { detail: `mock has no route for ${method} ${path}` }, latencyMs);
    }

    let result: MockResponse;
    try {
      result = await handler(recorded);
    } catch (err) {
      result = {
        status: 500,
        body: { detail: `mock route handler threw: ${err instanceof Error ? err.message : String(err)}` },
      };
    }
    return send(res, result.status, result.body, latencyMs, result.headers);
  }

  async function send(
    res: ServerResponse,
    status: number,
    body: unknown,
    delayMs: number,
    extraHeaders?: Record<string, string>,
  ): Promise<void> {
    if (delayMs > 0) await sleep(delayMs);
    const payload = typeof body === "string" ? body : JSON.stringify(body ?? {});
    res.writeHead(status, {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(payload),
      ...extraHeaders,
    });
    res.end(payload);
  }

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("mock cognee server did not report a port");
  }
  const url = `http://127.0.0.1:${address.port}`;

  return {
    url,
    requests,
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.close((err) => (err ? reject(err) : resolve()));
      }),
  };
}

export { MOCK_API_KEY, MOCK_JWT_TOKEN };
