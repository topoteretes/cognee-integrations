import test from "node:test";
import assert from "node:assert/strict";
import { CogneeClient, CogneeApiError } from "../dist/lib/index.js";
import { json } from "./helpers.mjs";

const make = (fetchImpl, extra = {}) => new CogneeClient({ baseUrl: "http://cognee.test:8000", apiKey: "k", fetch: fetchImpl, requestTimeoutMs: 2000, ...extra });

test("the deadline covers the response body: a stalled body times out instead of hanging", async () => {
  // Headers arrive at once; the body stream never closes and ignores the fetch signal.
  const stalled = () => new Response(new ReadableStream({ start() {} }), { status: 200, headers: { "Content-Type": "application/json" } });
  const client = make(async () => stalled(), { requestTimeoutMs: 20 });
  const started = Date.now();
  await assert.rejects(client.listDatasets({ retries: 0 }), (error) => error instanceof CogneeApiError && error.kind === "timeout");
  assert.ok(Date.now() - started < 500, `took ${Date.now() - started} ms`);
});

test("a body that completes within the deadline is read normally and the timer is cleared", async () => {
  const client = make(async () => json([{ id: "a", name: "x" }]), { requestTimeoutMs: 50 });
  assert.deepEqual(await client.listDatasets({ retries: 0 }), [{ id: "a", name: "x" }]);
  await new Promise((resolve) => setTimeout(resolve, 80));
});

test("document lookup distinguishes absent from exhausted; prefix listing reports completeness", async () => {
  const ds = "9b8c7d6e-0000-4000-8000-000000000002";
  const full = Array.from({ length: 100 }, (_, i) => ({ id: `${String(i).padStart(8, "0")}-0000-4000-8000-000000000000`, name: `issue-other-${i}` }));
  let pages = 0;
  const endless = make(async () => {
    pages += 1;
    return json(full);
  });
  const exhausted = await endless.findDataItemByName(ds, "issue-wanted", { maxPages: 3 });
  assert.deepEqual(exhausted, { status: "exhausted", pagesRead: 3 });
  assert.equal(pages, 3);
  const listing = await endless.listDataItemsByPrefix(ds, "issue-", { maxPages: 2 });
  assert.equal(listing.complete, false);
  assert.equal(listing.items.length, 200);

  const secondPage = full.slice(0, 10).map((item) => ({ ...item, name: item.name.replace("other", "second") }));
  const short = make(async (url) => (new URL(url).searchParams.get("offset") === "0" ? json(full) : json(secondPage)));
  assert.deepEqual(await short.findDataItemByName(ds, "issue-wanted"), { status: "absent" });
  const found = await short.findDataItemByName(ds, "issue-second-5");
  assert.equal(found.status, "found");
  assert.equal(found.item.name, "issue-second-5");
  const complete = await short.listDataItemsByPrefix(ds, "issue-");
  assert.equal(complete.complete, true);
  assert.equal(complete.items.length, 110);
});
