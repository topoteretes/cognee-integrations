/**
 * A real HTTP server standing in for Cognee.
 *
 * Deliberately a real `node:http` listener rather than a stubbed `global.fetch`.
 * `CogneeHttpClient` calls global `fetch` directly with no injectable transport,
 * so a stub would only prove "the method built some arguments" — it could not
 * exercise the parts that actually break: header assembly, status-code handling,
 * the 401 re-login retry, timeout/abort behaviour, or multipart bodies. Those all
 * live between the call and the wire, and a real socket is the only place they
 * are observable.
 *
 * Lives in `test-utils/` rather than `__tests__/` on purpose: jest's default
 * testMatch treats every file under `__tests__/` as a suite, so a helper there
 * fails the run with "no tests found".
 *
 * The client uses two path prefixes — `/api/v1/x` in local mode and bare `/x` in
 * cloud mode (`isCloud`) — so every route is registered under both. Tests that
 * care which one was used can assert on `calls`.
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";

/** One received request, recorded for assertion. */
export interface RecordedCall {
  method: string;
  /** Path with the `/api/v1` prefix stripped, so assertions are mode-agnostic. */
  route: string;
  /** The path exactly as sent, for tests that assert the prefix itself. */
  rawPath: string;
  query: Record<string, string>;
  headers: Record<string, string>;
  /** Parsed JSON body when the request sent JSON, else undefined. */
  json?: unknown;
  /** Raw body text, always present (multipart included). */
  body: string;
}

interface Forced {
  status: number;
  body: unknown;
  /** Consume after one match — for "fail once, then succeed" retry tests. */
  once?: boolean;
}

const DEFAULT_RESPONSES: Record<string, unknown> = {
  "POST /auth/login": { access_token: "test-token" },
  "GET /auth/api-keys": [{ key: "test-api-key", name: "default" }],
  "POST /auth/api-keys": { key: "minted-api-key" },
  "GET /health": { status: "ok" },
  "GET /datasets": [{ id: "ds-1", name: "testds" }],
  "POST /add": { datasetId: "ds-1" },
  "POST /remember": { datasetId: "ds-1", items: [] },
  "POST /remember/entry": { entryId: "e1" },
  "POST /recall": { results: [] },
  "POST /search": { results: [] },
  "POST /cognify": { status: "completed" },
  "POST /memify": { status: "completed" },
  "POST /improve": { status: "ok", dataset_id: "ds-1" },
  "POST /forget": { ok: true },
  "POST /agents/register": { ok: true, connectionId: "c1" },
  "POST /agents/unregister": { ok: true, activeAgents: 0 },
};

export class MockCognee {
  private server: Server;
  private forced = new Map<string, Forced[]>();
  private overrides = new Map<string, unknown>();

  /** Every request received, in order. */
  readonly calls: RecordedCall[] = [];

  /** Base URL to hand the client, e.g. `http://127.0.0.1:54321`. */
  url = "";

  private constructor(server: Server) {
    this.server = server;
  }

  static async start(): Promise<MockCognee> {
    const mock = new MockCognee(createServer());
    mock.server.on("request", (req, res) => void mock.handle(req, res));
    await new Promise<void>((resolve) => mock.server.listen(0, "127.0.0.1", resolve));
    const address = mock.server.address();
    if (!address || typeof address === "string") throw new Error("mock server has no port");
    mock.url = `http://127.0.0.1:${address.port}`;
    return mock;
  }

  async close(): Promise<void> {
    await new Promise<void>((resolve, reject) =>
      this.server.close((err) => (err ? reject(err) : resolve())),
    );
  }

  /** Forget recorded calls and any forced/overridden responses. */
  reset(): void {
    this.calls.length = 0;
    this.forced.clear();
    this.overrides.clear();
  }

  /** Replace the default body for a route, for the rest of the test. */
  setResponse(method: string, route: string, body: unknown): void {
    this.overrides.set(`${method.toUpperCase()} ${route}`, body);
  }

  /**
   * Make a route answer with `status` instead of its default.
   *
   * With `once`, only the next matching request is affected — which is what makes
   * the client's retry and 401-re-login paths testable: fail the first attempt,
   * then let the retry through.
   */
  forceResponse(method: string, route: string, status: number, body: unknown = {}, once = false): void {
    const key = `${method.toUpperCase()} ${route}`;
    const list = this.forced.get(key) ?? [];
    list.push({ status, body, once });
    this.forced.set(key, list);
  }

  /** Calls matching a method+route, prefix-agnostic. */
  callsTo(method: string, route: string): RecordedCall[] {
    return this.calls.filter((c) => c.method === method.toUpperCase() && c.route === route);
  }

  /** The first matching call, or throw with the full list — a readable failure. */
  assertCalled(method: string, route: string): RecordedCall {
    const found = this.callsTo(method, route)[0];
    if (!found) {
      const seen = this.calls.map((c) => `${c.method} ${c.rawPath}`).join(", ") || "(none)";
      throw new Error(`expected ${method.toUpperCase()} ${route}; received: ${seen}`);
    }
    return found;
  }

  assertNotCalled(method: string, route: string): void {
    const found = this.callsTo(method, route);
    if (found.length) throw new Error(`unexpected ${method.toUpperCase()} ${route} (${found.length}x)`);
  }

  private async handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(chunk as Buffer);
    const body = Buffer.concat(chunks).toString("utf8");

    const parsed = new URL(req.url ?? "/", "http://127.0.0.1");
    const rawPath = parsed.pathname;
    // Strip the local-mode prefix so assertions do not care which mode is under
    // test; `rawPath` is kept for the tests that specifically do.
    const route = rawPath.startsWith("/api/v1") ? rawPath.slice("/api/v1".length) || "/" : rawPath;

    let json: unknown;
    if (body && (req.headers["content-type"] ?? "").includes("application/json")) {
      try {
        json = JSON.parse(body);
      } catch {
        json = undefined;
      }
    }

    this.calls.push({
      method: (req.method ?? "GET").toUpperCase(),
      route,
      rawPath,
      query: Object.fromEntries(parsed.searchParams),
      headers: Object.fromEntries(
        Object.entries(req.headers).map(([k, v]) => [k, Array.isArray(v) ? v.join(",") : (v ?? "")]),
      ),
      json,
      body,
    });

    const key = `${(req.method ?? "GET").toUpperCase()} ${route}`;

    const queue = this.forced.get(key);
    if (queue?.length) {
      const forced = queue[0];
      if (forced.once) queue.shift();
      else this.forced.set(key, queue);
      return this.send(res, forced.status, forced.body);
    }

    if (this.overrides.has(key)) return this.send(res, 200, this.overrides.get(key));

    const fallback = DEFAULT_RESPONSES[key];
    if (fallback !== undefined) return this.send(res, 200, fallback);

    // Dynamic routes: /datasets/<id>/data and /datasets/<id>/data/<dataId>.
    if (/^\/datasets\/[^/]+\/data$/.test(route)) return this.send(res, 200, [{ id: "data-1", name: "f.md" }]);
    if (/^\/datasets\/[^/]+\/data\/[^/]+$/.test(route)) return this.send(res, 200, { ok: true });
    if (/^\/datasets\/[^/]+\/status$/.test(route)) return this.send(res, 200, { status: "completed" });

    // 404 rather than a permissive 200: an unimplemented route should surface as
    // a test failure naming the path, not as a silently empty success.
    return this.send(res, 404, { detail: `mock has no route for ${req.method} ${rawPath}` });
  }

  private send(res: ServerResponse, status: number, body: unknown): void {
    const payload = typeof body === "string" ? body : JSON.stringify(body ?? {});
    res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) });
    res.end(payload);
  }
}
