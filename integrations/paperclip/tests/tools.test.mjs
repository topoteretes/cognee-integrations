import test from "node:test";
import assert from "node:assert/strict";
import { boot, issue, COMPANY, OTHER_COMPANY, PROJECT, PROJECT_2, PROJECT_OFF, BASE_CONFIG, fixture } from "./helpers.mjs";

const ISSUE_ID = issue().id;

test("cognee_recall searches only the run's permitted datasets and ignores dataset selectors in params", async () => {
  const { harness, fake, ledger, restore } = await boot({ seed: { issues: [issue()] }, config: { ...BASE_CONFIG, companySharedDataset: "northwind_shared" } });
  try {
    await harness.emit("issue.updated", {}, { entityId: ISSUE_ID, entityType: "issue", companyId: COMPANY });
    const row = (await ledger.list())[0];
    fake.state.datasets.push({ id: "9b8c7d6e-0000-4000-8000-000000000077", name: "northwind_shared" });
    fake.state.recall = () => [
      { kind: "chunk", text: row.content, score: 0.9, dataset_name: row.dataset, metadata: { document_name: row.uploadName, data_id: "x" } },
      { kind: "chunk", text: "LEAKED from another project", score: 0.95, dataset_name: "pc_0000000000000000" },
    ];
    const result = await harness.executeTool("cognee_recall", { query: "onboarding password", datasets: ["pc_0000000000000000"], company_id: OTHER_COMPANY }, { companyId: COMPANY, projectId: PROJECT });
    assert.equal(result.error, undefined);
    assert.match(result.content, /historical evidence/);
    assert.match(result.content, /https:\/\/paperclip\.example\.com\/issues\//);
    assert.ok(!result.content.includes("LEAKED"));
    assert.equal(result.data.evidence.length, 1);
    const recallCall = fake.calls.filter((c) => c.path === "/api/v1/recall").at(-1);
    assert.deepEqual(recallCall.body.datasets, [row.dataset, "northwind_shared"]);
    assert.deepEqual(recallCall.body.scope, ["graph"]);
    assert.equal(recallCall.body.search_type, "CHUNKS");
  } finally {
    restore();
  }
});

test("cross-company and non-enabled runs are denied; a disabled project with no shared dataset has no scope", async () => {
  const { harness, fake, restore } = await boot({ seed: { issues: [issue()] } });
  try {
    const denied = await harness.executeTool("cognee_recall", { query: "anything" }, { companyId: OTHER_COMPANY, projectId: PROJECT });
    assert.match(denied.error, /not enabled/);
    const noScope = await harness.executeTool("cognee_recall", { query: "anything" }, { companyId: COMPANY, projectId: PROJECT_OFF });
    assert.equal(noScope.error, undefined);
    assert.equal(noScope.data.status, "empty");
    assert.equal(fake.calls.filter((c) => c.path === "/api/v1/recall").length, 0);
    const empty = await harness.executeTool("cognee_recall", { query: "" }, { companyId: COMPANY, projectId: PROJECT });
    assert.match(empty.error, /query is required/);
  } finally {
    restore();
  }
});

test("recall distinguishes an empty scope from a backend failure", async () => {
  const { harness, fake, restore } = await boot({ seed: { issues: [issue()] } });
  try {
    const nothing = await harness.executeTool("cognee_recall", { query: "anything" }, { companyId: COMPANY, projectId: PROJECT });
    assert.equal(nothing.data.status, "empty");
    await harness.emit("issue.updated", {}, { entityId: ISSUE_ID, entityType: "issue", companyId: COMPANY });
    fake.state.recall = () => {
      throw new TypeError("fetch failed");
    };
    const down = await harness.executeTool("cognee_recall", { query: "anything" }, { companyId: COMPANY, projectId: PROJECT });
    assert.match(down.error, /unreachable/);
    fake.state.recall = fixture("recall-dataset-not-found.json").body; // handled by the fake as 200 JSON; use an override instead
  } finally {
    restore();
  }
});

test("cognee_remember_decision records a decision tied to an authorized issue and sends it; replays dedupe", async () => {
  const { harness, fake, ledger, restore } = await boot({ seed: { issues: [issue()] } });
  try {
    const result = await harness.executeTool("cognee_remember_decision", { content: "Ship magic-link login first.", rationale: "RES-1 interviews", source_issue_id: ISSUE_ID }, { companyId: COMPANY, projectId: PROJECT, agentId: "agent-9", runId: "run-9" });
    assert.equal(result.error, undefined);
    assert.match(result.content, /Recorded decision for task RES-1 \(receipt /);
    assert.equal(result.data.status, "accepted");
    const row = await ledger.get(result.data.receiptId);
    assert.equal(row.sourceType, "decision");
    assert.match(row.content, /Recorded by agent agent-9 in run run-9/);
    assert.match(row.content, /Rationale:\nRES-1 interviews/);
    const again = await harness.executeTool("cognee_remember_decision", { content: "Ship magic-link login first.", rationale: "RES-1 interviews", source_issue_id: ISSUE_ID }, { companyId: COMPANY, projectId: PROJECT, agentId: "agent-9", runId: "run-9" });
    assert.equal(again.data.created, false);
    assert.equal(again.data.receiptId, result.data.receiptId);
    assert.equal(fake.calls.filter((c) => c.path === "/api/v1/remember").length, 1);
  } finally {
    restore();
  }
});

test("cognee_remember_decision refuses issues outside the company, outside enabled projects, or in another project than the run", async () => {
  const foreign = issue({ id: "66666666-aaaa-4aaa-8aaa-666666666666", companyId: OTHER_COMPANY });
  const off = issue({ id: "77777777-aaaa-4aaa-8aaa-777777777777", projectId: PROJECT_OFF });
  const { harness, ledger, restore } = await boot({ seed: { issues: [issue(), foreign, off] } });
  try {
    const a = await harness.executeTool("cognee_remember_decision", { content: "x", source_issue_id: foreign.id }, { companyId: COMPANY, projectId: PROJECT });
    assert.match(a.error, /does not name an issue in this company/);
    const b = await harness.executeTool("cognee_remember_decision", { content: "x", source_issue_id: off.id }, { companyId: COMPANY, projectId: PROJECT_OFF });
    assert.match(b.error, /not enabled/);
    const c = await harness.executeTool("cognee_remember_decision", { content: "x", source_issue_id: ISSUE_ID }, { companyId: COMPANY, projectId: PROJECT_2 });
    assert.match(c.error, /different project/);
    const d = await harness.executeTool("cognee_remember_decision", { content: "x" }, { companyId: COMPANY, projectId: PROJECT });
    assert.match(d.error, /source_issue_id is required/);
    const e = await harness.executeTool("cognee_remember_decision", { content: "x", source_issue_id: ISSUE_ID }, { companyId: OTHER_COMPANY, projectId: PROJECT });
    assert.match(e.error, /not enabled/);
    assert.equal((await ledger.list()).length, 0);
  } finally {
    restore();
  }
});

test("memory-health data reports counts and the last drain", async () => {
  const { harness, restore } = await boot({ seed: { issues: [issue()] } });
  try {
    await harness.emit("issue.updated", {}, { entityId: ISSUE_ID, entityType: "issue", companyId: COMPANY });
    const health = await harness.getData("memory-health", { companyId: COMPANY });
    assert.equal(health.counts.accepted, 1);
    assert.equal(health.version, "0.1.0");
  } finally {
    restore();
  }
});
