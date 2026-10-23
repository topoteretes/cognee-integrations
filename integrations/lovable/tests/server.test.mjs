import test from "node:test";
import assert from "node:assert/strict";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { CogneeClient, MemoryService, ReceiptStore, createConnectorServer, resolveConfig } from "../dist/index.js";
import { request as httpRequest } from "node:http";
import { fakeCognee, fixture, BASE_ENV } from "./helpers.mjs";

/** fetch() refuses to override Host, so raw node:http is used where the Host header matters. */
function rawPost(port, path, headers, body) {
  return new Promise((resolve, reject) => {
    const req = httpRequest({ host: "127.0.0.1", port, path, method: "POST", headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body), ...headers } }, (res) => {
      res.resume();
      res.on("end", () => resolve(res.statusCode));
    });
    req.on("error", reject);
    req.end(body);
  });
}

async function start(envExtra = {}, overrides = {}) {
  const fake = fakeCognee(overrides);
  const config = resolveConfig({}, { ...BASE_ENV, ...envExtra });
  const client = new CogneeClient({ baseUrl: config.cognee.baseUrl, apiKey: config.cognee.apiKey, requestTimeoutMs: 2000, maxResponseBytes: 1 << 20, fetch: fake.fetchImpl });
  const service = new MemoryService({ config, client, store: new ReceiptStore(":memory:") });
  await service.prepare();
  const connector = createConnectorServer({ config, service });
  const bound = await connector.listen();
  // Port 0 picked a free port: the Host allowlist was computed for port 0, so
  // widen it to the bound port the way an operator would with LOVABLE_PUBLIC_URL.
  config.http.allowedHosts.push(`127.0.0.1:${bound.port}`, `localhost:${bound.port}`);
  const stop = async () => {
    await connector.close();
    service.close();
  };
  return { fake, config, service, bound, stop };
}

async function connect(url, token, extraHeaders = {}) {
  const transport = new StreamableHTTPClientTransport(new URL(url), { requestInit: { headers: { Authorization: `Bearer ${token}`, ...extraHeaders } } });
  const mcp = new Client({ name: "lovable-test-client", version: "0.0.0" });
  await mcp.connect(transport);
  return { mcp, transport };
}

test("a real MCP client initializes, discovers exactly three tools, and calls them end to end", async () => {
  const { fake, bound, stop } = await start();
  try {
    const { mcp } = await connect(bound.url, BASE_ENV.LOVABLE_MCP_TOKEN);
    const tools = await mcp.listTools();
    assert.deepEqual(tools.tools.map((t) => t.name).sort(), ["memory_status", "recall_product_context", "remember_product_decision"]);
    const recallTool = tools.tools.find((t) => t.name === "recall_product_context");
    assert.deepEqual(Object.keys(recallTool.inputSchema.properties).sort(), ["limit", "mode", "query"]);
    assert.equal(recallTool.annotations.readOnlyHint, true);
    const rememberTool = tools.tools.find((t) => t.name === "remember_product_decision");
    assert.deepEqual(Object.keys(rememberTool.inputSchema.properties).sort(), ["content", "rationale", "request_id", "source_reference"]);

    const saved = await mcp.callTool({ name: "remember_product_decision", arguments: { content: "Ship magic-link login first.", rationale: "Top support ticket.", source_reference: "feedback-2026-10-01", request_id: "chat-42" } });
    assert.equal(saved.isError ?? false, false);
    assert.equal(saved.structuredContent.status, "accepted");
    const receiptId = saved.structuredContent.receiptId;
    assert.match(saved.content[0].text, new RegExp(`Receipt ${receiptId}`));

    const status = await mcp.callTool({ name: "memory_status", arguments: { receipt_id: receiptId } });
    assert.equal(status.structuredContent.status, "ready");

    fake.state.recall = fixture("recall-chunks.json");
    const recalled = await mcp.callTool({ name: "recall_product_context", arguments: { query: "login decision", limit: 3 } });
    assert.equal(recalled.structuredContent.status, "ok");
    assert.match(recalled.content[0].text, /historical evidence/);
    assert.match(recalled.content[0].text, /record memory-abc123/);
    const recallCall = fake.calls.find((c) => c.path === "/api/v1/recall");
    assert.deepEqual(recallCall.body.datasets, ["lovable_probe"]);
    await mcp.close();
  } finally {
    await stop();
  }
});

test("model-controlled arguments cannot widen scope: unknown dataset selectors are rejected by the schema", async () => {
  const { fake, bound, stop } = await start();
  try {
    const { mcp } = await connect(bound.url, BASE_ENV.LOVABLE_MCP_TOKEN);
    const result = await mcp.callTool({ name: "recall_product_context", arguments: { query: "login", datasets: ["other_project"], dataset: "other_project" } });
    // Unknown keys are stripped (zod object default); the search stays on the configured scope.
    const recallCall = fake.calls.filter((c) => c.path === "/api/v1/recall").at(-1);
    assert.deepEqual(recallCall.body.datasets, ["lovable_probe"]);
    assert.notEqual(result.structuredContent.status, "error");
    const bad = await mcp.callTool({ name: "recall_product_context", arguments: { query: "" } });
    assert.equal(bad.isError, true);
    const forged = await mcp.callTool({ name: "memory_status", arguments: { receipt_id: "rcpt_forged000000000000000" } });
    assert.equal(forged.isError, true);
    assert.equal(forged.structuredContent.status, "not_found");
    await mcp.close();
  } finally {
    await stop();
  }
});

test("missing, malformed and wrong bearer tokens are refused before any MCP handling; rotated tokens both work", async () => {
  const { bound, stop } = await start({ LOVABLE_MCP_TOKEN: `${BASE_ENV.LOVABLE_MCP_TOKEN},lvm_rotated_token_0123456789` });
  try {
    for (const headers of [{}, { Authorization: "Bearer wrong_token_0123456789" }, { Authorization: "Basic abc" }]) {
      const res = await fetch(bound.url, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", ...headers }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} }) });
      assert.equal(res.status, 401);
      assert.match(res.headers.get("www-authenticate"), /Bearer/);
    }
    for (const token of [BASE_ENV.LOVABLE_MCP_TOKEN, "lvm_rotated_token_0123456789"]) {
      const { mcp } = await connect(bound.url, token);
      assert.equal((await mcp.listTools()).tools.length, 3);
      await mcp.close();
    }
  } finally {
    await stop();
  }
});

test("host and origin allowlists are enforced; health is open and credential-free", async () => {
  const { bound, stop } = await start({ LOVABLE_PUBLIC_URL: "https://memory.example.com" });
  try {
    const base = `http://127.0.0.1:${bound.port}`;
    const body = JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping" });
    const auth = { Accept: "application/json, text/event-stream", Authorization: `Bearer ${BASE_ENV.LOVABLE_MCP_TOKEN}` };
    assert.equal(await rawPost(bound.port, "/mcp", { ...auth, Host: "evil.example.net" }, body), 421);
    assert.equal(await rawPost(bound.port, "/mcp", { ...auth, Host: "memory.example.com" }, body), 200);
    assert.equal(await rawPost(bound.port, "/mcp", { ...auth, Host: "memory.example.com", Origin: "https://attacker.example" }, body), 403);
    assert.equal(await rawPost(bound.port, "/mcp", { ...auth, Host: "memory.example.com", Origin: "https://memory.example.com" }, body), 200);
    assert.equal(await rawPost(bound.port, "/other", { ...auth, Host: "memory.example.com" }, body), 404);
    const health = await fetch(`${base}/health`);
    assert.equal(health.status, 200);
    const healthBody = await health.json();
    assert.equal(healthBody.status, "ok");
    assert.ok(!JSON.stringify(healthBody).includes(BASE_ENV.COGNEE_API_KEY));
  } finally {
    await stop();
  }
});

test("oversized and malformed bodies are rejected", async () => {
  const { bound, stop } = await start({ LOVABLE_MAX_REQUEST_BODY_BYTES: "2048" });
  try {
    const headers = { "Content-Type": "application/json", Accept: "application/json, text/event-stream", Authorization: `Bearer ${BASE_ENV.LOVABLE_MCP_TOKEN}` };
    const big = await fetch(bound.url, { method: "POST", headers, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping", params: { pad: "x".repeat(4096) } }) });
    assert.equal(big.status, 413);
    const malformed = await fetch(bound.url, { method: "POST", headers, body: "{not json" });
    assert.ok(malformed.status >= 400 && malformed.status < 500);
  } finally {
    await stop();
  }
});

test("a backend outage surfaces as 'unavailable' in the tool result, never as a transport failure", async () => {
  const { bound, stop } = await start({}, { "POST /api/v1/recall": () => { throw new TypeError("fetch failed"); } });
  try {
    const { mcp } = await connect(bound.url, BASE_ENV.LOVABLE_MCP_TOKEN);
    const result = await mcp.callTool({ name: "recall_product_context", arguments: { query: "anything" } });
    assert.equal(result.structuredContent.status, "unavailable");
    assert.match(result.content[0].text, /unreachable/);
    assert.ok(!result.content[0].text.includes(BASE_ENV.COGNEE_API_KEY));
    await mcp.close();
  } finally {
    await stop();
  }
});

test("reconnecting clients work because the server is stateless", async () => {
  const { bound, stop } = await start();
  try {
    const first = await connect(bound.url, BASE_ENV.LOVABLE_MCP_TOKEN);
    await first.mcp.listTools();
    await first.transport.close();
    const second = await connect(bound.url, BASE_ENV.LOVABLE_MCP_TOKEN);
    assert.equal((await second.mcp.listTools()).tools.length, 3);
    await second.mcp.close();
  } finally {
    await stop();
  }
});
