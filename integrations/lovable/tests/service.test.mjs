import test from "node:test";
import assert from "node:assert/strict";
import { CogneeClient, MemoryService, ReceiptStore, resolveConfig } from "../dist/index.js";
import { fakeCognee, fixture, json, BASE_ENV } from "./helpers.mjs";

function build(envExtra = {}, overrides = {}) {
  const fake = fakeCognee(overrides);
  const config = resolveConfig({}, { ...BASE_ENV, ...envExtra });
  const client = new CogneeClient({ baseUrl: config.cognee.baseUrl, apiKey: config.cognee.apiKey, requestTimeoutMs: 2000, maxResponseBytes: 1 << 20, fetch: fake.fetchImpl });
  let now = Date.parse("2026-10-23T12:00:00Z");
  const clock = () => new Date(now);
  const service = new MemoryService({ config, client, store: new ReceiptStore(":memory:", { clock }), clock });
  return { fake, config, service, advance: (ms) => { now += ms; } };
}

test("prepare creates the project dataset and reports missing shared datasets without creating them", async () => {
  const { fake, service } = build({ LOVABLE_SHARED_DATASETS: "shared_research,missing_one" });
  const report = await service.prepare();
  assert.equal(report.backendReachable, true);
  assert.equal(report.projectDatasetId, fake.datasetId);
  assert.deepEqual(report.sharedDatasets, [{ name: "shared_research", available: true }, { name: "missing_one", available: false }]);
  assert.ok(report.problems.some((p) => /missing_one/.test(p)));
  assert.ok(!fake.state.datasets.some((d) => d.name === "missing_one"));
  assert.ok(report.problems.some((p) => /in-memory/.test(p)));
});

test("recall searches only the configured datasets and filters out-of-scope results", async () => {
  const { fake, service } = build({ LOVABLE_SHARED_DATASETS: "shared_research" });
  await service.prepare();
  fake.state.recall = () => [
    ...fixture("recall-chunks.json"),
    { kind: "chunk", text: "LEAKED", score: 0.99, dataset_id: "x", dataset_name: "other_project" },
    { kind: "chunk", text: "shared evidence", score: 0.4, dataset_id: "y", dataset_name: "shared_research" },
  ];
  const result = await service.recall({ query: "login decision", limit: 5 });
  assert.equal(result.status, "ok");
  assert.deepEqual(result.datasetsSearched, ["lovable_probe", "shared_research"]);
  assert.equal(result.droppedOutOfScope, 1);
  assert.deepEqual(result.evidence.map((e) => e.dataset), ["lovable_probe", "shared_research"]);
  assert.ok(!JSON.stringify(result).includes("LEAKED"));
  assert.equal(result.evidence[0].documentName, "memory-abc123");
  assert.equal(result.evidence[0].dataId, "d03f564c-ff7b-4810-ac63-c27b1c37138a");
  const call = fake.calls.find((c) => c.path === "/api/v1/recall");
  assert.deepEqual(call.body.datasets, ["lovable_probe", "shared_research"]);
  assert.equal(call.body.search_type, "CHUNKS");
});

test("recall bounds results and characters and reports truncation", async () => {
  const { fake, service } = build({ LOVABLE_MAX_RECALL_CHARS: "50", LOVABLE_MAX_RECALL_RESULTS: "2", LOVABLE_RECALL_TOP_K: "2" });
  await service.prepare();
  fake.state.recall = () => Array.from({ length: 5 }, (_, i) => ({ kind: "chunk", text: `passage ${i} ${"x".repeat(40)}`, score: 1 - i / 10, dataset_name: "lovable_probe" }));
  const result = await service.recall({ query: "anything", limit: 99 });
  assert.equal(result.status, "ok");
  assert.ok(result.evidence.length <= 2);
  assert.ok(result.evidence.reduce((n, e) => n + e.text.length, 0) <= 51);
  assert.equal(result.truncated, true);
});

test("recall answer mode is labelled, uses GRAPH_COMPLETION, and can be disabled", async () => {
  const { fake, service } = build();
  await service.prepare();
  fake.state.recall = () => fixture("recall-graph-completion.json");
  const result = await service.recall({ query: "What did we decide about login?", mode: "answer" });
  assert.equal(result.status, "ok");
  assert.match(result.answer, /passwordless magic-link/);
  assert.equal(fake.calls.find((c) => c.path === "/api/v1/recall").body.search_type, "GRAPH_COMPLETION");
  const disabled = build({ LOVABLE_RECALL_ALLOW_ANSWER_MODE: "false" });
  await disabled.service.prepare();
  const refused = await disabled.service.recall({ query: "q", mode: "answer" });
  assert.equal(refused.status, "error");
  assert.match(refused.message, /disabled/);
});

test("recall on an empty project (dataset not created yet) is 'empty', never an unscoped search", async () => {
  const { fake, service } = build({ LOVABLE_PROJECT_DATASET: "brand_new" });
  // No prepare(): the dataset does not exist until the first write.
  const result = await service.recall({ query: "anything" });
  assert.equal(result.status, "empty");
  assert.match(result.message, /nothing has been remembered/);
  const recalls = fake.calls.filter((c) => c.path === "/api/v1/recall");
  assert.ok(recalls.every((c) => Array.isArray(c.body.datasets) && c.body.datasets.length > 0));
});

test("a completion search on an emptied dataset (404 NoDataError) is 'empty', not an error", async () => {
  const { service } = build({}, { "POST /api/v1/recall": () => json({ detail: "NoDataError: No searchable memory in dataset 'lovable_probe' (id: 9c18f560-50a7-5665-9bb3-15aee16a3447): no data has been added; add data and run cognify before searching. (Status code: 404)" }, 404) });
  await service.prepare();
  const result = await service.recall({ query: "anything", mode: "answer" });
  assert.equal(result.status, "empty");
});

test("recall failures are distinguishable: auth, outage, timeout", async () => {
  const unauthorized = build({}, { "POST /api/v1/recall": () => json(fixture("unauthorized.json").body, 401) });
  await unauthorized.service.prepare();
  const a = await unauthorized.service.recall({ query: "q" });
  assert.equal(a.status, "unavailable");
  assert.match(a.message, /credentials/);
  const down = build({}, { "POST /api/v1/recall": () => { throw new TypeError("fetch failed"); } });
  await down.service.prepare();
  const b = await down.service.recall({ query: "q" });
  assert.equal(b.status, "unavailable");
  assert.match(b.message, /unreachable/);
});

test("remember writes a provenance-bearing document, returns a receipt, and dedupes by request id and content", async () => {
  const { fake, service } = build();
  await service.prepare();
  const first = await service.rememberDecision({ content: "Use magic-link login.", rationale: "12 interviews", sourceReference: "feedback-2026-10-01", requestId: "req-1" });
  assert.equal(first.status, "accepted");
  assert.match(first.receiptId, /^rcpt_/);
  assert.equal(first.deduplicated, false);
  const upload = fake.calls.find((c) => c.path === "/api/v1/remember");
  const file = upload.body.get("data");
  assert.match(file.name, /^decision-[0-9a-f]{32}\.txt$/);
  const text = await file.text();
  assert.match(text, /Source reference: feedback-2026-10-01/);
  assert.match(text, /Decision:\nUse magic-link login\./);
  assert.match(text, /Rationale:\n12 interviews/);
  assert.match(text, /Recorded: 2026-10-23T12:00:00/);

  const replay = await service.rememberDecision({ content: "Something else", requestId: "req-1" });
  assert.equal(replay.receiptId, first.receiptId);
  assert.equal(replay.deduplicated, true);
  const sameContent = await service.rememberDecision({ content: "Use magic-link login.", rationale: "12 interviews", sourceReference: "feedback-2026-10-01" });
  assert.equal(sameContent.receiptId, first.receiptId);
  assert.equal(sameContent.deduplicated, true);
  assert.equal(fake.calls.filter((c) => c.path === "/api/v1/remember").length, 1);
});

test("remember bounds inputs and refuses empty content", async () => {
  const { fake, service } = build({ LOVABLE_MAX_CONTENT_CHARS: "20" });
  await service.prepare();
  const empty = await service.rememberDecision({ content: "   " });
  assert.equal(empty.status, "failed");
  assert.equal(empty.receiptId, "");
  const long = await service.rememberDecision({ content: "x".repeat(500) });
  assert.equal(long.status, "accepted");
  const text = await fake.calls.find((c) => c.path === "/api/v1/remember").body.get("data").text();
  assert.ok(!text.includes("x".repeat(21)));
});

test("a timed-out write is 'unknown' and later reconciles to ready once the document appears", async () => {
  let fail = true;
  const { fake, service, advance } = build({}, {
    "POST /api/v1/remember": ({ body, state }) => {
      if (fail) throw Object.assign(new Error("aborted"), { name: "AbortError" });
      const name = body.get("data").name.replace(/\.txt$/, "");
      state.data.push({ id: "d03f564c-ff7b-4810-ac63-c27b1c37138a", name, datasetId: state.datasets[0].id });
      return json(fixture("remember-background.json"));
    },
  });
  await service.prepare();
  const result = await service.rememberDecision({ content: "Decision A", requestId: "r-a" });
  assert.equal(result.status, "unknown");
  assert.match(result.message, /outcome is unknown/);
  let status = await service.status(result.receiptId);
  assert.equal(status.status, "unknown");
  // Simulate the server having stored it after all (the write landed late).
  fake.state.data.push({ id: "d03f564c-ff7b-4810-ac63-c27b1c37138a", name: service.store.get(result.receiptId).uploadName, datasetId: fake.datasetId });
  status = await service.status(result.receiptId);
  assert.equal(status.status, "ready");
  assert.equal(status.dataId, "d03f564c-ff7b-4810-ac63-c27b1c37138a");
  advance(1000);
  fail = false;
});

test("status moves accepted → indexing → ready, and failed on a pipeline error", async () => {
  const { fake, service } = build();
  await service.prepare();
  fake.state.pipeline = "DATASET_PROCESSING_STARTED";
  const result = await service.rememberDecision({ content: "Decision B" });
  let status = await service.status(result.receiptId);
  assert.equal(status.status, "indexing");
  fake.state.pipeline = "DATASET_PROCESSING_COMPLETED";
  status = await service.status(result.receiptId);
  assert.equal(status.status, "ready");
  assert.equal((await service.status("rcpt_doesnotexist")).status, "not_found");

  const errored = build();
  await errored.service.prepare();
  const r2 = await errored.service.rememberDecision({ content: "Decision C" });
  errored.fake.state.pipeline = "DATASET_PROCESSING_ERRORED";
  assert.equal((await errored.service.status(r2.receiptId)).status, "failed");
});

test("a document that never appears becomes 'unknown' after the grace period, and reconcile covers all pending receipts", async () => {
  const { fake, service, advance } = build({}, { "POST /api/v1/remember": () => json(fixture("remember-background.json")) });
  await service.prepare();
  const r = await service.rememberDecision({ content: "Vanishing decision" });
  assert.equal((await service.status(r.receiptId)).status, "indexing");
  advance(16 * 60 * 1000);
  const summary = await service.reconcile();
  assert.equal(summary.checked, 1);
  assert.deepEqual(summary.byStatus, { unknown: 1 });
  assert.equal(fake.state.data.length, 0);
});

test("forget deletes on the backend, verifies by re-listing, clears content, and cannot be undone by reconcile", async () => {
  const { fake, service } = build();
  await service.prepare();
  const r = await service.rememberDecision({ content: "Deletable decision", rationale: "secret-ish" });
  assert.equal((await service.status(r.receiptId)).status, "ready");
  const result = await service.forget(r.receiptId);
  assert.equal(result.verified, true);
  assert.equal(result.status, "deleted");
  const forgetCall = fake.calls.find((c) => c.path === "/api/v1/forget");
  assert.equal(forgetCall.body.datasetId, fake.datasetId);
  assert.match(forgetCall.body.dataId, /^d03f564c/);
  const receipt = service.store.get(r.receiptId);
  assert.equal(receipt.content, null);
  assert.equal(receipt.rationale, null);
  await service.reconcile();
  assert.equal(service.store.get(r.receiptId).status, "deleted");
  assert.equal((await service.status(r.receiptId)).status, "deleted");
});

test("forget refuses to mark deleted when the backend still lists the document", async () => {
  const { service } = build({}, { "POST /api/v1/forget": ({ body }) => json({ data_id: body.dataId, dataset_id: body.datasetId, status: "success" }) });
  await service.prepare();
  const r = await service.rememberDecision({ content: "Sticky decision" });
  await service.status(r.receiptId);
  const result = await service.forget(r.receiptId);
  assert.equal(result.verified, false);
  assert.match(result.message, /still lists/);
  assert.notEqual(service.store.get(r.receiptId).status, "deleted");
});

test("concurrent writes with different content get distinct receipts", async () => {
  const { service } = build();
  await service.prepare();
  const results = await Promise.all(["A", "B", "C"].map((c) => service.rememberDecision({ content: `Decision ${c}` })));
  assert.equal(new Set(results.map((r) => r.receiptId)).size, 3);
  assert.ok(results.every((r) => r.status === "accepted"));
});

test("identical decisions saved at different times are one record", async () => {
  const { fake, service, advance } = build();
  await service.prepare();
  const first = await service.rememberDecision({ content: "Use magic-link login.", rationale: "12 interviews" });
  advance(1000);
  const second = await service.rememberDecision({ content: "Use magic-link login.", rationale: "12 interviews" });
  assert.equal(second.deduplicated, true);
  assert.equal(second.receiptId, first.receiptId);
  assert.equal(fake.calls.filter((c) => c.path === "/api/v1/remember").length, 1);
  // The stored document still records when it was captured.
  const text = await fake.calls.find((c) => c.path === "/api/v1/remember").body.get("data").text();
  assert.match(text, /Recorded: 2026-10-23T12:00:00/);
});

test("an exhausted dataset listing never counts as absence", async () => {
  const { fake, service } = build({}, { "POST /api/v1/remember": () => json(fixture("remember-background.json")) });
  await service.prepare();
  for (let i = 0; i < 150; i += 1) fake.state.data.push({ id: `bbbbbbbb-0000-4000-8000-${String(i).padStart(12, "0")}`, name: `other-${i}`, datasetId: fake.datasetId });
  const original = service.client.findDataItemByName.bind(service.client);
  service.client.findDataItemByName = (datasetId, name, opts = {}) => original(datasetId, name, { ...opts, maxPages: 1 });
  const r = await service.rememberDecision({ content: "Hidden decision" });
  const status = await service.status(r.receiptId);
  assert.equal(status.status, "accepted");
  assert.match(service.store.get(r.receiptId).error, /listing exhausted/);
  service.store.update(r.receiptId, { dataId: "d03f564c-ff7b-4810-ac63-000000000099" });
  const result = await service.forget(r.receiptId);
  assert.equal(result.verified, false);
  assert.match(result.message, /too long to verify/);
  assert.notEqual(service.store.get(r.receiptId).status, "deleted");
});
