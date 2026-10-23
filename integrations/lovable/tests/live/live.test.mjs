/**
 * Opt-in live acceptance against a real cognee server (COGNEE_LIVE=1,
 * COGNEE_BASE_URL, COGNEE_API_KEY). Uses a disposable dataset and deletes
 * what it wrote. Run with `npm run test:live`.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { MemoryService, ReceiptStore, createConnectorServer, resolveConfig } from "../../dist/index.js";

const live = process.env.COGNEE_LIVE === "1" && process.env.COGNEE_BASE_URL && process.env.COGNEE_API_KEY;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test("live: save a decision over MCP, wait for ready, recall it from a fresh client, delete it, confirm it is gone", { skip: !live && "set COGNEE_LIVE=1, COGNEE_BASE_URL, COGNEE_API_KEY" }, async () => {
  const dataset = `lovable_live_${randomBytes(4).toString("hex")}`;
  const token = `lvm_live_${randomBytes(16).toString("hex")}`;
  const config = resolveConfig({}, { ...process.env, LOVABLE_PROJECT_DATASET: dataset, LOVABLE_MCP_TOKEN: token, LOVABLE_RECEIPTS_PATH: ":memory:", LOVABLE_MCP_PORT: "0" });
  const service = new MemoryService({ config, store: new ReceiptStore(":memory:") });
  const report = await service.prepare();
  assert.equal(report.backendReachable, true, JSON.stringify(report.problems));
  const connector = createConnectorServer({ config, service });
  const bound = await connector.listen();
  config.http.allowedHosts.push(`127.0.0.1:${bound.port}`);
  const open = async () => {
    const transport = new StreamableHTTPClientTransport(new URL(bound.url), { requestInit: { headers: { Authorization: `Bearer ${token}` } } });
    const mcp = new Client({ name: "live", version: "0" });
    await mcp.connect(transport);
    return mcp;
  };
  try {
    const marker = `Zebra-${randomBytes(3).toString("hex")}`;
    const writer = await open();
    const saved = await writer.callTool({ name: "remember_product_decision", arguments: { content: `Decision: the checkout page uses the ${marker} colour palette.`, rationale: "Usability study on 2026-10-20 preferred it.", source_reference: "study-2026-10-20" } });
    assert.equal(saved.structuredContent.status, "accepted", saved.content[0].text);
    const receiptId = saved.structuredContent.receiptId;
    await writer.close();

    let status;
    for (let i = 0; i < 60; i += 1) {
      status = await service.status(receiptId);
      if (status.status === "ready" || status.status === "failed") break;
      await sleep(5000);
    }
    assert.equal(status.status, "ready", status.message);

    const reader = await open();
    let found = false;
    for (let i = 0; i < 6 && !found; i += 1) {
      const recalled = await reader.callTool({ name: "recall_product_context", arguments: { query: `checkout page colour palette ${marker}` } });
      found = recalled.structuredContent.status === "ok" && recalled.content[0].text.includes(marker);
      if (!found) await sleep(5000);
    }
    assert.ok(found, "recall did not return the saved decision");
    await reader.close();

    const deleted = await service.forget(receiptId);
    assert.equal(deleted.verified, true, deleted.message);
    const after = await service.recall({ query: `checkout page colour palette ${marker}` });
    assert.ok(["empty", "ok"].includes(after.status), after.message);
    assert.ok(!after.evidence.some((item) => item.text.includes(marker)), "deleted record still recalled");
  } finally {
    await connector.close();
    try {
      await service.client.forgetDocument; // no-op: dataset-wide deletion is not exposed by the service by design
      const ds = (await service.client.listDatasets()).find((d) => d.name === dataset);
      if (ds) await fetch(`${config.cognee.baseUrl}/api/v1/forget`, { method: "POST", headers: { "Content-Type": "application/json", "X-Api-Key": config.cognee.apiKey }, body: JSON.stringify({ datasetId: ds.id }) });
    } catch {
      /* best effort cleanup */
    }
    service.close();
  }
});
