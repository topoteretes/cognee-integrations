import test from "node:test";
import assert from "node:assert/strict";
import { boot, issue, json, fixture, COMPANY, PROJECT, BASE_CONFIG, API_KEY } from "./helpers.mjs";

const ISSUE_ID = issue().id;

async function queued({ overrides, clock, config } = {}) {
  const booted = await boot({ seed: { issues: [issue()] }, overrides, clock, config, drainOnEvent: false });
  await booted.harness.emit("issue.updated", {}, { entityId: ISSUE_ID, entityType: "issue", companyId: COMPANY });
  return booted;
}

test("the drain job sends pending rows per company and records the dataset id; readiness moves accepted → indexing → ready", async () => {
  const { harness, fake, ledger, restore } = await queued();
  try {
    assert.equal((await ledger.list())[0].status, "pending");
    await harness.runJob("drain-outbox");
    let row = (await ledger.list())[0];
    assert.equal(row.status, "accepted");
    assert.match(row.datasetId, /^[0-9a-f-]{36}$/);
    assert.ok(harness.getState({ scopeKind: "instance", stateKey: "last-drain" }).sent === 1);
    fake.state.pipeline = "DATASET_PROCESSING_STARTED";
    await harness.runJob("reconcile-sources");
    row = (await ledger.list())[0];
    assert.equal(row.status, "indexing");
    assert.ok(row.dataId);
    fake.state.pipeline = "DATASET_PROCESSING_COMPLETED";
    await harness.runJob("reconcile-sources");
    assert.equal((await ledger.list())[0].status, "ready");
  } finally {
    restore();
  }
});

test("401 fails the row for an operator, 409 is a conflict, 429 and 5xx retry then fail, timeouts stay uncertain", async () => {
  const cases = [
    { name: "401", respond: () => json(fixture("unauthorized.json").body, 401), expect: "failed", drains: 1 },
    { name: "409", respond: () => json({ error: "conflict" }, 409), expect: "conflict", drains: 1 },
    { name: "429 then give up", respond: () => json({ detail: "slow down" }, 429), expect: "failed", drains: 6 },
    { name: "500 once", respond: (n) => (n === 0 ? json({ detail: "boom" }, 500) : null), expect: "accepted", drains: 2 },
    { name: "timeout", respond: () => { throw Object.assign(new Error("aborted"), { name: "AbortError" }); }, expect: "uncertain", drains: 1 },
    { name: "network", respond: () => { throw new TypeError("fetch failed"); }, expect: "uncertain", drains: 1 },
  ];
  for (const c of cases) {
    let n = 0;
    const booted = await queued({
      overrides: {
        "POST /api/v1/remember": async (call) => {
          const forced = c.respond(n++);
          if (forced) return forced;
          const file = call.body.get("data");
          const ds = { id: "9b8c7d6e-0000-4000-8000-000000000002", name: call.body.get("datasetName") };
          call.state.datasets.push(ds);
          call.state.data.push({ id: "d03f564c-ff7b-4810-ac63-c27b1c37138a", name: file.name.replace(/\.txt$/, ""), datasetId: ds.id, text: await file.text() });
          return json({ ...fixture("remember-background.json"), dataset_id: ds.id });
        },
      },
    });
    try {
      for (let i = 0; i < c.drains; i += 1) await booted.harness.runJob("drain-outbox");
      const row = (await booted.ledger.list())[0];
      assert.equal(row.status, c.expect, `${c.name}: ${row.error}`);
      const health = await booted.plugin.definition.onHealth();
      if (c.expect === "failed" || c.expect === "conflict") assert.equal(health.status, "degraded", c.name);
      else assert.equal(health.status, "ok", c.name);
    } finally {
      booted.restore();
    }
  }
});

test("an uncertain write whose document never appears is resent after the grace period with the same upload name", async () => {
  let now = Date.parse("2026-10-23T12:00:00Z");
  let fail = true;
  const { harness, fake, ledger, restore } = await queued({
    clock: () => new Date(now),
    overrides: {
      "POST /api/v1/remember": async (call) => {
        if (fail) throw Object.assign(new Error("aborted"), { name: "AbortError" });
        const file = call.body.get("data");
        let ds = call.state.datasets.find((d) => d.name === call.body.get("datasetName"));
        if (!ds) {
          ds = { id: "9b8c7d6e-0000-4000-8000-000000000002", name: call.body.get("datasetName") };
          call.state.datasets.push(ds);
        }
        call.state.data.push({ id: "d03f564c-ff7b-4810-ac63-c27b1c37138a", name: file.name.replace(/\.txt$/, ""), datasetId: ds.id, text: await file.text() });
        return json({ ...fixture("remember-background.json"), dataset_id: ds.id });
      },
    },
  });
  try {
    await harness.runJob("drain-outbox");
    assert.equal((await ledger.list())[0].status, "uncertain");
    await harness.runJob("reconcile-sources");
    assert.equal((await ledger.list())[0].status, "uncertain", "inside the grace period nothing changes");
    now += 16 * 60 * 1000;
    await harness.runJob("reconcile-sources");
    assert.equal((await ledger.list())[0].status, "pending");
    fail = false;
    await harness.runJob("drain-outbox");
    await harness.runJob("reconcile-sources");
    const row = (await ledger.list())[0];
    assert.equal(row.status, "ready");
    const names = fake.calls.filter((c) => c.path === "/api/v1/remember").map((c) => c.body.get("data").name);
    assert.equal(new Set(names).size, 1, "resend reuses the content-derived upload name");
  } finally {
    restore();
  }
});

test("a source deleted in Paperclip is retired on reconcile", async () => {
  const { harness, fake, ledger, restore } = await queued();
  try {
    await harness.runJob("drain-outbox");
    await harness.runJob("reconcile-sources");
    assert.equal((await ledger.list())[0].status, "ready");
    // Re-seed the harness without the issue: the source is gone.
    harness.seed({ issues: [issue({ id: "99999999-aaaa-4aaa-8aaa-999999999999", identifier: "OTHER" })] });
    const removed = (await ledger.list())[0];
    // The harness keeps seeded issues; emulate deletion by pointing the row at an unknown issue.
    await ledger.update(removed.id, {});
    const stillThere = await harness.ctx.issues.get(ISSUE_ID, COMPANY);
    if (stillThere) {
      // Fall back: run reconcile with a row whose source id does not exist.
      const { row } = await ledger.upsert({ ...removed, sourceId: "00000000-aaaa-4aaa-8aaa-000000000000", revision: "deadbeef", uploadName: "issue-deadbeef", content: "Task id: x" });
      await ledger.update(row.id, { status: "ready", datasetId: removed.datasetId, dataId: removed.dataId });
      await harness.runJob("reconcile-sources");
      assert.equal((await ledger.get(row.id)).status, "retired");
    } else {
      await harness.runJob("reconcile-sources");
      assert.equal((await ledger.get(removed.id)).status, "retired");
    }
    assert.ok(fake.calls.some((c) => c.path === "/api/v1/forget"));
  } finally {
    restore();
  }
});

test("cleanup-scope revokes queued writes first, deletes only that project's plugin-owned documents, and verifies", async () => {
  const other = issue({ id: "44444444-aaaa-4aaa-8aaa-444444444444", identifier: "PROD-1", projectId: "7b2d4c3e-5f6a-4b7c-9d8e-0f1a2b3c4d5e" });
  const { harness, fake, ledger, restore } = await boot({ seed: { issues: [issue(), other] }, drainOnEvent: false });
  try {
    await harness.emit("issue.updated", {}, { entityId: ISSUE_ID, entityType: "issue", companyId: COMPANY });
    await harness.emit("issue.updated", {}, { entityId: other.id, entityType: "issue", companyId: COMPANY });
    await harness.runJob("drain-outbox");
    await harness.runJob("reconcile-sources");
    assert.equal((await ledger.list({ status: "ready" })).length, 2);
    // A new pending revision that must never be sent after cleanup.
    harness.seed({ issues: [issue({ description: "late edit" })] });
    await harness.emit("issue.updated", {}, { entityId: ISSUE_ID, entityType: "issue", companyId: COMPANY });
    assert.equal((await ledger.list({ status: "pending" })).length, 1);
    await assert.rejects(harness.performAction("cleanup-scope", { projectId: PROJECT }, { companyId: COMPANY }), /confirm/);
    const summary = await harness.performAction("cleanup-scope", { projectId: PROJECT, confirm: true }, { companyId: COMPANY });
    assert.equal(summary.revoked, 2, "the pending revision and the superseded old row are revoked");
    assert.equal(summary.deleted, 1);
    assert.equal(summary.verifiedAbsent, 1);
    assert.equal(summary.stillPresent, 0);
    await harness.runJob("drain-outbox");
    assert.equal(fake.calls.filter((c) => c.path === "/api/v1/remember").length, 2, "nothing is sent after cleanup");
    const otherRow = (await ledger.list({ projectId: other.projectId }))[0];
    assert.equal(otherRow.status, "ready", "the other project is untouched");
    assert.equal(fake.state.data.length, 1);
  } finally {
    restore();
  }
});

test("disabling a company holds its queued rows; disabling a project revokes them", async () => {
  const { harness, fake, ledger, restore } = await queued();
  try {
    harness.setConfig({ ...BASE_CONFIG, enabledCompanyIds: [] });
    await harness.runJob("drain-outbox");
    assert.equal((await ledger.list())[0].status, "pending");
    harness.setConfig({ ...BASE_CONFIG, enabledProjectIds: [] });
    await harness.runJob("drain-outbox");
    assert.equal((await ledger.list())[0].status, "revoked");
    assert.equal(fake.calls.filter((c) => c.path === "/api/v1/remember").length, 0);
  } finally {
    restore();
  }
});

test("validateConfig reaches the backend with the configured credentials", async () => {
  const { plugin, restore } = await boot({ seed: {} });
  try {
    const ok = await plugin.definition.onValidateConfig(BASE_CONFIG);
    assert.equal(ok.ok, true);
    const bad = await plugin.definition.onValidateConfig({ ...BASE_CONFIG, cogneeBaseUrl: "not a url" });
    assert.equal(bad.ok, false);
    assert.match(bad.errors.join(" "), /cogneeBaseUrl/);
  } finally {
    restore();
  }
});

test("secrets are resolved per call and never written to the ledger or logs", async () => {
  const { harness, ledger, restore } = await queued();
  try {
    await harness.runJob("drain-outbox");
    const dump = JSON.stringify(await ledger.list()) + JSON.stringify(harness.logs) + JSON.stringify(harness.activity);
    assert.ok(!dump.includes(API_KEY));
  } finally {
    restore();
  }
});
