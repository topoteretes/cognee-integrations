/**
 * The HTTP front door: `POST /mcp` (Streamable HTTP, stateless: one MCP
 * server + transport per request, so any replica can answer and a missing
 * session never yields a 400), `GET /health`, everything else 404. Before a
 * request reaches the MCP layer it must pass the Host allowlist, the Origin
 * check (when an Origin is present), the bearer token, and the body size cap.
 */
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { createBearerAuthenticator, type Authenticator } from "./auth.js";
import type { LovableMemoryConfig } from "./config.js";
import type { Logger } from "./logger.js";
import { silentLogger } from "./logger.js";
import type { MemoryService } from "./memory.js";
import { createMcpServer } from "./tools.js";
import { PACKAGE_NAME, PACKAGE_VERSION } from "./version.js";

export interface ConnectorServerOptions {
  config: LovableMemoryConfig;
  service: MemoryService;
  logger?: Logger;
}

export interface ConnectorServer {
  server: Server;
  /** Bind and resolve with the actual port (0 picks a free one). */
  listen(): Promise<{ host: string; port: number; url: string }>;
  close(): Promise<void>;
}

function json(res: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) });
  res.end(payload);
}

function hostAllowed(config: LovableMemoryConfig, hostHeader: string | undefined): boolean {
  if (config.http.allowedHosts.includes("*")) return true;
  if (!hostHeader) return false;
  return config.http.allowedHosts.includes(hostHeader.trim().toLowerCase());
}

function originAllowed(config: LovableMemoryConfig, originHeader: string | undefined): boolean {
  if (originHeader === undefined) return true; // non-browser clients send none
  if (config.http.allowedOrigins.includes("*")) return true;
  return config.http.allowedOrigins.includes(originHeader.trim().toLowerCase());
}

export function createConnectorServer(options: ConnectorServerOptions): ConnectorServer {
  const { config, service } = options;
  const logger = options.logger ?? silentLogger;
  const authenticate: Authenticator = createBearerAuthenticator(config.tokens);
  const mcpPath = config.http.path;

  const handler = async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    const started = Date.now();
    const url = new URL(req.url ?? "/", "http://placeholder");
    const method = req.method ?? "GET";
    const finish = (status: number, extra?: Record<string, unknown>) =>
      logger.info("request", { method, path: url.pathname, status, ms: Date.now() - started, ...(extra ?? {}) });

    try {
      if (url.pathname === "/health" && method === "GET") {
        json(res, 200, { status: "ok", service: PACKAGE_NAME, version: PACKAGE_VERSION });
        finish(200);
        return;
      }
      if (url.pathname !== mcpPath) {
        json(res, 404, { error: "not found" });
        finish(404);
        return;
      }
      if (!hostAllowed(config, req.headers.host)) {
        json(res, 421, { error: "host not allowed; set LOVABLE_PUBLIC_URL or LOVABLE_ALLOWED_HOSTS to the hostname Lovable uses" });
        finish(421, { reason: "host" });
        return;
      }
      if (!originAllowed(config, req.headers.origin)) {
        json(res, 403, { error: "origin not allowed" });
        finish(403, { reason: "origin" });
        return;
      }
      const decision = authenticate(req.headers.authorization);
      if (!decision.ok) {
        res.setHeader("WWW-Authenticate", 'Bearer realm="lovable-memory"');
        json(res, 401, { error: "unauthorized" });
        finish(401, { reason: decision.reason });
        return;
      }
      if (method !== "POST" && method !== "GET" && method !== "DELETE") {
        res.setHeader("Allow", "POST, GET, DELETE");
        json(res, 405, { error: "method not allowed" });
        finish(405);
        return;
      }

      const mcp = createMcpServer(service);
      const transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: undefined, // stateless
        maxRequestBodySize: config.http.maxRequestBodyBytes,
      });
      res.on("close", () => {
        void transport.close().catch(() => undefined);
        void mcp.close().catch(() => undefined);
      });
      await mcp.connect(transport);
      await transport.handleRequest(req, res);
      finish(res.statusCode, { tokenIndex: decision.tokenIndex });
    } catch (error) {
      logger.error("request failed", { method, path: url.pathname, error: (error as Error)?.message ?? String(error) });
      if (!res.headersSent) json(res, 500, { error: "internal error" });
      else res.end();
    }
  };

  const server = createServer((req, res) => {
    void handler(req, res);
  });
  server.requestTimeout = 120_000;
  server.headersTimeout = 30_000;

  return {
    server,
    listen: () =>
      new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(config.http.port, config.http.host, () => {
          server.off("error", reject);
          const address = server.address();
          const port = typeof address === "object" && address ? address.port : config.http.port;
          const host = config.http.host;
          const displayHost = host.includes(":") ? `[${host}]` : host;
          resolve({ host, port, url: `http://${displayHost}:${port}${mcpPath}` });
        });
      }),
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections?.();
        server.close(() => resolve());
      }),
  };
}
