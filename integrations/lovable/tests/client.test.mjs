import test from "node:test";
import assert from "node:assert/strict";
import { CogneeClient, CogneeApiError } from "../dist/index.js";
import { fakeCognee, fixture, json, BASE_ENV } from "./helpers.mjs";

const make = (fetchImpl, extra = {}) =>
  new CogneeClient({ baseUrl: "http://cognee.test:8000/", apiKey: BASE_ENV.COGNEE_API_KEY, requestTimeoutMs: 2000, maxResponseBytes: 1024 * 1024, fetch: fetchImpl, ...extra });

test("remember uploads a content-named file with datasetName and run_in_background", async () => {
  const fake = fakeCognee();
  const client = make(fake.fetchImpl);
  const result = await client.remember({ datasetName: "lovable_probe", filename: "decision-abc.txt", text: "hello" });
  assert.equal(result.status, "running");
  assert.equal(result.dataset_id, fake.datasetId);
  const call = fake.calls.find((c) => c.path === "/api/v1/remember");
  assert.equal(call.headers["X-Api-Key"], BASE_ENV.COGNEE_API_KEY);
  assert.equal(call.body.get("data").name, "decision-abc.txt");
  assert.equal(call.body.get("run_in_background"), "true");
  assert.equal(call.body.get("datasetName"), "lovable_probe");
});

test("recall always states graph scope and explicit datasets", async () => {
  const fake = fakeCognee();
  const client = make(fake.fetchImpl);
  const items = await client.recall({ query: "login", datasets: ["lovable_probe"], searchType: "CHUNKS", topK: 3 });
  assert.equal(items.length, 1);
  assert.equal(items[0].metadata.data_id, "d03f564c-ff7b-4810-ac63-c27b1c37138a");
  const call = fake.calls.find((c) => c.path === "/api/v1/recall");
  assert.deepEqual(call.body, { query: "login", datasets: ["lovable_probe"], scope: ["graph"], search_type: "CHUNKS", top_k: 3 });
  await assert.rejects(client.recall({ query: "x", datasets: [], searchType: "CHUNKS", topK: 1 }), /at least one dataset/);
});

test("HTTP errors carry status and the server's detail; 401 is auth, 404 is not-found", async () => {
  const client = make(async () => json(fixture("unauthorized.json").body, 401));
  await assert.rejects(client.listDatasets(), (error) => {
    assert.ok(error instanceof CogneeApiError);
    assert.equal(error.status, 401);
    assert.ok(error.isAuth);
    assert.match(error.detail, /Authentication required/);
    assert.ok(!error.outcomeUncertain);
    return true;
  });
  const notFound = make(async () => json(fixture("recall-dataset-not-found.json").body, 404));
  await assert.rejects(notFound.recall({ query: "x", datasets: ["nope"], searchType: "CHUNKS", topK: 1 }), (error) => error.isNotFound);
});

test("an error-bearing 2xx body is an error", async () => {
  const client = make(async () => json({ error: "An error occurred during processing." }));
  await assert.rejects(client.ensureDataset("x"), (error) => error instanceof CogneeApiError && /processing/.test(error.detail));
});

test("timeouts and network failures are uncertain outcomes; writes are not retried, reads are", async () => {
  let attempts = 0;
  const flaky = make(async () => {
    attempts += 1;
    if (attempts === 1) throw new TypeError("fetch failed");
    return json([]);
  });
  assert.deepEqual(await flaky.listDatasets(), []);
  assert.equal(attempts, 2);

  attempts = 0;
  const failingWrite = make(async () => {
    attempts += 1;
    throw new TypeError("fetch failed");
  });
  await assert.rejects(failingWrite.remember({ datasetName: "d", filename: "f.txt", text: "t" }), (error) => error.kind === "network" && error.outcomeUncertain);
  assert.equal(attempts, 1);

  const slow = make((_url, init) => new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })))), { requestTimeoutMs: 30 });
  await assert.rejects(slow.remember({ datasetName: "d", filename: "f.txt", text: "t" }), (error) => error.kind === "timeout" && error.outcomeUncertain);
});

test("redirects are not followed with credentials", async () => {
  const client = make(async () => new Response("", { status: 307, headers: { Location: "https://elsewhere.example/api" } }));
  await assert.rejects(client.listDatasets(), (error) => error.kind === "redirect" && error.status === 307);
});

test("oversized responses are refused", async () => {
  const client = make(async () => new Response("x".repeat(2048), { status: 200, headers: { "Content-Type": "application/json", "Content-Length": "2048" } }), { maxResponseBytes: 1024 });
  await assert.rejects(client.listDatasets(), /too large/);
});

test("dataset status unwraps the single-pipeline shape; forget needs UUIDs", async () => {
  const fake = fakeCognee();
  const client = make(fake.fetchImpl);
  assert.equal(await client.datasetStatus(fake.datasetId), "DATASET_PROCESSING_COMPLETED");
  await assert.rejects(client.forgetDocument("not-a-uuid", fake.datasetId), /not a UUID/);
  await assert.rejects(client.datasetStatus("../../etc"), /not a UUID/);
});

test("findDataItemByName pages through the listing and stops at a short page", async () => {
  const fake = fakeCognee();
  for (let i = 0; i < 150; i += 1) fake.state.data.push({ id: `aaaaaaaa-0000-4000-8000-${String(i).padStart(12, "0")}`, name: `doc-${i}`, datasetId: fake.datasetId });
  const client = make(fake.fetchImpl);
  const hit = await client.findDataItemByName(fake.datasetId, "doc-149");
  assert.equal(hit.name, "doc-149");
  assert.equal(await client.findDataItemByName(fake.datasetId, "doc-999"), undefined);
  const pages = fake.calls.filter((c) => c.path.includes("/data?")).length;
  assert.ok(pages >= 2 && pages <= 4, `expected 2-4 listing pages, got ${pages}`);
});
