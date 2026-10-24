import test from "node:test";
import assert from "node:assert/strict";
import { boot, issue, comment, COMPANY, OTHER_COMPANY, PROJECT, PROJECT_OFF, BASE_CONFIG } from "./helpers.mjs";

const ISSUE_ID = issue().id;

test("a done issue in an enabled project is captured once across replayed and reordered events, and sent immediately", async () => {
  const { harness, fake, ledger, restore } = await boot({ seed: { issues: [issue()] } });
  try {
    const event = { entityId: ISSUE_ID, entityType: "issue", companyId: COMPANY };
    await harness.emit("issue.updated", { issueId: ISSUE_ID, status: "done" }, { ...event, eventId: "e1" });
    await harness.emit("issue.updated", { issueId: ISSUE_ID, status: "done" }, { ...event, eventId: "e1" }); // replay
    await harness.emit("issue.updated", { issueId: ISSUE_ID, status: "in_progress" }, { ...event, eventId: "e0", occurredAt: "2026-10-01T00:00:00Z" }); // stale, out of order
    const rows = await ledger.list();
    assert.equal(rows.length, 1);
    assert.equal(rows[0].sourceType, "issue");
    assert.equal(rows[0].status, "accepted");
    assert.match(rows[0].dataset, /^pc_[0-9a-f]{16}$/);
    const uploads = fake.calls.filter((c) => c.path === "/api/v1/remember");
    assert.equal(uploads.length, 1);
    assert.equal(uploads[0].body.get("data").name, `${rows[0].uploadName}.txt`);
    assert.equal(uploads[0].body.get("datasetName"), rows[0].dataset);
    assert.equal(uploads[0].headers["X-Api-Key"], "ck_test_key_0123456789");
    assert.ok(harness.activity.some((a) => /Queued completed task RES-1/.test(a.message)));
  } finally {
    restore();
  }
});

test("non-done issues, disabled projects, other companies and captureMode off are ignored", async () => {
  const cases = [
    { name: "not done", seed: { issues: [issue({ status: "in_review" })] }, config: BASE_CONFIG },
    { name: "project not enabled", seed: { issues: [issue({ projectId: PROJECT_OFF })] }, config: BASE_CONFIG },
    { name: "company not enabled", seed: { issues: [issue({ companyId: OTHER_COMPANY })] }, config: BASE_CONFIG, companyId: OTHER_COMPANY },
    { name: "capture off", seed: { issues: [issue()] }, config: { ...BASE_CONFIG, captureMode: "off" } },
    { name: "comments only", seed: { issues: [issue()] }, config: { ...BASE_CONFIG, captureMode: "marked_comments" } },
  ];
  for (const c of cases) {
    const { harness, fake, ledger, restore } = await boot({ seed: c.seed, config: c.config });
    try {
      await harness.emit("issue.updated", { issueId: ISSUE_ID }, { entityId: ISSUE_ID, entityType: "issue", companyId: c.companyId ?? COMPANY });
      assert.equal((await ledger.list()).length, 0, c.name);
      assert.equal(fake.calls.filter((x) => x.path === "/api/v1/remember").length, 0, c.name);
    } finally {
      restore();
    }
  }
});

test("only comments that start with the marker are captured, with their issue context", async () => {
  const marked = comment();
  const plain = comment({ id: "c2c2c2c2-eeee-4eee-8eee-c2c2c2c2c2c2", body: "just a note, please #remember nothing" });
  const { harness, ledger, restore } = await boot({ seed: { issues: [issue()], issueComments: [marked, plain] } });
  try {
    for (const c of [marked, plain]) {
      await harness.emit("issue.comment.created", { issueId: ISSUE_ID, commentId: c.id }, { entityId: c.id, entityType: "issue_comment", companyId: COMPANY });
    }
    const rows = await ledger.list();
    assert.equal(rows.length, 1);
    assert.equal(rows[0].sourceType, "comment");
    assert.equal(rows[0].sourceId, marked.id);
    assert.match(rows[0].content, /Task: RES-1/);
    assert.match(rows[0].content, /Decision:\nPassword friction/);
    assert.equal(rows[0].status, "accepted");
  } finally {
    restore();
  }
});

test("a comment event without an issue id, or for an issue in another company, captures nothing", async () => {
  const { harness, ledger, restore } = await boot({ seed: { issues: [issue()], issueComments: [comment()] } });
  try {
    await harness.emit("issue.comment.created", { commentId: comment().id }, { entityId: comment().id, entityType: "issue_comment", companyId: COMPANY });
    await harness.emit("issue.comment.created", { issueId: ISSUE_ID, commentId: comment().id }, { entityId: comment().id, entityType: "issue_comment", companyId: OTHER_COMPANY });
    assert.equal((await ledger.list()).length, 0);
  } finally {
    restore();
  }
});

test("an edited source gets a new revision; the old document is retired (verified) before the new one is sent", async () => {
  const { harness, fake, ledger, runtime, restore } = await boot({ seed: { issues: [issue()] } });
  try {
    await harness.emit("issue.updated", {}, { entityId: ISSUE_ID, entityType: "issue", companyId: COMPANY });
    const first = (await ledger.list())[0];
    await runtime.outbox.reconcileAll(); // learn the data id
    assert.equal((await ledger.get(first.id)).status, "ready");
    harness.seed({ issues: [issue({ description: "Findings revised: 10 of 12 customers abandoned at the password step." })] });
    await harness.emit("issue.updated", {}, { entityId: ISSUE_ID, entityType: "issue", companyId: COMPANY });
    const rows = await ledger.list();
    assert.equal(rows.length, 2);
    const old = rows.find((r) => r.id === first.id);
    const fresh = rows.find((r) => r.id !== first.id);
    assert.equal(old.status, "retired");
    assert.equal(old.content, null);
    assert.equal(fresh.status, "accepted");
    const order = fake.calls.filter((c) => c.path === "/api/v1/forget" || c.path === "/api/v1/remember").map((c) => c.path);
    assert.deepEqual(order, ["/api/v1/remember", "/api/v1/forget", "/api/v1/remember"]);
    assert.equal(fake.state.data.length, 1);
    assert.equal(fake.state.data[0].name, fresh.uploadName);
  } finally {
    restore();
  }
});

test("when the old document cannot be retired the replacement is held, not sent", async () => {
  const { harness, fake, ledger, runtime, restore } = await boot({ seed: { issues: [issue()] } });
  try {
    await harness.emit("issue.updated", {}, { entityId: ISSUE_ID, entityType: "issue", companyId: COMPANY });
    await runtime.outbox.reconcileAll();
    fake.state.failForget = true; // forget "succeeds" but the document stays listed
    harness.seed({ issues: [issue({ description: "revised again" })] });
    await harness.emit("issue.updated", {}, { entityId: ISSUE_ID, entityType: "issue", companyId: COMPANY });
    const rows = await ledger.list();
    const old = rows.find((r) => r.sourceType === "issue" && r.status !== "pending");
    const fresh = rows.find((r) => r.status === "pending");
    assert.equal(old.status, "conflict");
    assert.ok(fresh, "replacement row stays pending");
    assert.equal(fake.calls.filter((c) => c.path === "/api/v1/remember").length, 1);
    const health = await runtime.outbox.ledger.countByStatus();
    assert.equal(health.conflict, 1);
  } finally {
    restore();
  }
});

test("sync-project backfills every done issue of an enabled project and refuses a disabled one", async () => {
  const done2 = issue({ id: "44444444-aaaa-4aaa-8aaa-444444444444", identifier: "RES-2", title: "Second" });
  const open = issue({ id: "55555555-aaaa-4aaa-8aaa-555555555555", identifier: "RES-3", status: "todo" });
  const { harness, ledger, restore } = await boot({ seed: { issues: [issue(), done2, open] } });
  try {
    const result = await harness.performAction("sync-project", { projectId: PROJECT }, { companyId: COMPANY });
    assert.deepEqual(result, { queued: 2, skipped: 0, alreadyRecorded: 0 });
    assert.equal((await ledger.list({ status: "accepted" })).length, 2);
    const again = await harness.performAction("sync-project", { projectId: PROJECT }, { companyId: COMPANY });
    assert.equal(again.alreadyRecorded, 2);
    await assert.rejects(harness.performAction("sync-project", { projectId: PROJECT_OFF }, { companyId: COMPANY }), /not enabled/);
  } finally {
    restore();
  }
});
