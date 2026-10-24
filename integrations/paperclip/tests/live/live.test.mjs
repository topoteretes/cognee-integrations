/**
 * Opt-in live acceptance against a real cognee server (COGNEE_LIVE=1,
 * COGNEE_BASE_URL, COGNEE_API_KEY) through the SDK test harness: a done
 * issue is captured, drained, polled to ready, recalled by the tool from a
 * fresh "run", then the scope is cleaned up and verified gone.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { createTestHarness } from "@paperclipai/plugin-sdk/testing";
import { createPlugin, InMemoryLedger, manifest } from "../../dist/lib/index.js";

const live = process.env.COGNEE_LIVE === "1" && process.env.COGNEE_BASE_URL && process.env.COGNEE_API_KEY;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test("live: capture → drain → ready → recall from a fresh run → cleanup", { skip: !live && "set COGNEE_LIVE=1, COGNEE_BASE_URL, COGNEE_API_KEY" }, async () => {
  const company = "0d9f2a4e-7c1b-4b6e-9a3f-5e2d8c1a7b10";
  const project = "6a1c3b2d-4e5f-4a6b-8c7d-9e0f1a2b3c4d";
  const marker = `Quokka-${randomBytes(3).toString("hex")}`;
  const config = {
    cogneeBaseUrl: process.env.COGNEE_BASE_URL,
    cogneeApiKeyRef: { type: "secret_ref", secretId: "live" },
    enabledCompanyIds: [company],
    enabledProjectIds: [project],
    instanceKey: `paperclip_live_${randomBytes(4).toString("hex")}`,
    paperclipPublicUrl: "https://paperclip.example.com",
  };
  const harness = createTestHarness({ manifest, config });
  harness.ctx.secrets = { resolve: async () => process.env.COGNEE_API_KEY };
  const issueId = "11111111-aaaa-4aaa-8aaa-111111111111";
  harness.seed({
    companies: [{ id: company, name: "Northwind", status: "active" }],
    projects: [{ id: project, companyId: company, name: "Research", status: "active" }],
    issues: [{ id: issueId, companyId: company, projectId: project, title: `Interview customers about the ${marker} onboarding flow`, identifier: "RES-1", status: "done", description: `Findings: customers abandon the ${marker} flow at the password step; 9 of 12 interviews cited password friction.`, workMode: "standard", priority: "medium", projectWorkspaceId: null, goalId: null, parentId: null, reviewPolicy: null, assigneeAgentId: null, assigneeUserId: null, checkoutRunId: null, executionRunId: null, executionAgentNameKey: null, executionLockedAt: null, createdByAgentId: null, createdByUserId: null, responsibleUserId: null, issueNumber: 1, createdAt: new Date(), updatedAt: new Date() }],
  });
  const ledger = new InMemoryLedger();
  const { plugin, runtime } = createPlugin({ ledger });
  await plugin.definition.setup(harness.ctx);
  try {
    await harness.emit("issue.updated", {}, { entityId: issueId, entityType: "issue", companyId: company });
    let row = (await ledger.list())[0];
    assert.ok(row, "a ledger row was created");
    if (row.status === "pending") await harness.runJob("drain-outbox");
    row = (await ledger.list())[0];
    assert.equal(row.status, "accepted", row.error ?? "");

    for (let i = 0; i < 60 && row.status !== "ready" && row.status !== "failed"; i += 1) {
      await sleep(5000);
      await harness.runJob("reconcile-sources");
      row = (await ledger.list())[0];
    }
    assert.equal(row.status, "ready", row.error ?? "");

    let found = false;
    for (let i = 0; i < 6 && !found; i += 1) {
      const result = await harness.executeTool("cognee_recall", { query: `${marker} onboarding password friction` }, { companyId: company, projectId: project, agentId: "agent-fresh", runId: `run-${i}` });
      assert.equal(result.error, undefined, result.error);
      found = result.data.status === "ok" && result.content.includes(marker);
      if (!found) await sleep(5000);
    }
    assert.ok(found, "recall did not return the captured task");

    const summary = await harness.performAction("cleanup-scope", { projectId: project, confirm: true }, { companyId: company });
    assert.equal(summary.deleted, 1, JSON.stringify(summary));
    assert.equal(summary.verifiedAbsent, 1);
    assert.equal((await ledger.get(row.id)).status, "retired");
  } finally {
    try {
      const backend = await runtime().backend(company);
      const ds = (await backend.client.listDatasets()).find((d) => d.name === backend.scope.projectDataset(company, project));
      if (ds) await fetch(`${config.cogneeBaseUrl}/api/v1/forget`, { method: "POST", headers: { "Content-Type": "application/json", "X-Api-Key": process.env.COGNEE_API_KEY }, body: JSON.stringify({ datasetId: ds.id }) });
    } catch {
      /* best effort */
    }
  }
});
