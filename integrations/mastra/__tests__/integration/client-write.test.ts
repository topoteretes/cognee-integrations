/**
 * `CogneeClient` write paths: `/remember` -> `/add`+`/cognify` fallback;
 * `/remember/entry` -> `/remember` fallback; multipart body shape for
 * `remember()`/`add()`; `ensureDataset()`'s dataset-id cache.
 *
 * Real `node:http` mock server throughout — every assertion reads the actual
 * bytes/headers the client put on the wire, not fetch call arguments.
 */

import { CogneeClient } from "../../src/client.js";
import { getCapabilities } from "../../src/capabilities.js";
import { startMockCognee, MOCK_API_KEY, type MockCognee } from "../test-utils/mock-cognee.js";
import { DATASET_ID, DATASET_NAME } from "../test-utils/fixtures.js";

let mock: MockCognee;

afterEach(async () => {
  if (mock) await mock.close();
});

// ---------------------------------------------------------------------------
// /remember -> /add + /cognify fallback
// ---------------------------------------------------------------------------

describe("/remember -> /add+/cognify fallback", () => {
  it("falls back to add()+cognify() on a 404, and never re-probes /remember afterward", async () => {
    mock = await startMockCognee({
      routes: { "POST /api/v1/remember": () => ({ status: 404, body: { detail: "no such route" } }) },
    });
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    const first = await client.remember({ raw_data: ["fact one"], datasetName: "ds1", session_id: "sess-1" });
    expect(first.status).toBe("completed");

    const second = await client.remember({ raw_data: ["fact two"], datasetName: "ds1", session_id: "sess-1" });
    expect(second.status).toBe("completed");

    // Exactly one probe of /remember, ever.
    expect(mock.requests.filter((r) => r.path === "/api/v1/remember").length).toBe(1);
    // Both writes went through the fallback leg.
    expect(mock.requests.filter((r) => r.path === "/api/v1/add").length).toBe(2);
    expect(mock.requests.filter((r) => r.path === "/api/v1/cognify").length).toBe(2);

    expect(getCapabilities(mock.url).rememberSupported).toBe(false);
  });

  it("uses /remember directly, and never calls /add or /cognify, when /remember works", async () => {
    mock = await startMockCognee();
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    await client.remember({ raw_data: ["fact"], datasetName: "ds1" });

    expect(mock.requests.filter((r) => r.path === "/api/v1/remember").length).toBe(1);
    expect(mock.requests.some((r) => r.path === "/api/v1/add")).toBe(false);
    expect(mock.requests.some((r) => r.path === "/api/v1/cognify")).toBe(false);
    expect(getCapabilities(mock.url).rememberSupported).toBe(true);
  });

  it("cognify() in the fallback leg is called with the dataset id add() returned", async () => {
    mock = await startMockCognee({
      routes: {
        "POST /api/v1/remember": () => ({ status: 404, body: { detail: "no such route" } }),
        "POST /api/v1/add": () => ({ status: 200, body: { status: "completed", dataset_id: "ds-from-add" } }),
      },
    });
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    await client.remember({ raw_data: ["fact"], datasetName: "ds1" });

    const cognifyReq = mock.requests.find((r) => r.path === "/api/v1/cognify");
    expect(cognifyReq).toBeDefined();
    expect((cognifyReq!.json as Record<string, unknown>).dataset_ids).toEqual(["ds-from-add"]);
  });
});

// ---------------------------------------------------------------------------
// /remember/entry -> /remember fallback
// ---------------------------------------------------------------------------

describe("/remember/entry -> /remember fallback", () => {
  it("falls back to remember() (turn serialized as raw_data) on a 404, and never re-probes /remember/entry", async () => {
    mock = await startMockCognee({
      routes: { "POST /api/v1/remember/entry": () => ({ status: 404, body: { detail: "no such route" } }) },
    });
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    const result = await client.rememberEntry({
      entry: { type: "qa", question: "What does the user prefer?", answer: "Dark mode." },
      dataset_name: "ds1",
      session_id: "sess-1",
    });
    expect(result.status).toBe("completed");

    await client.rememberEntry({
      entry: { type: "qa", question: "second question", answer: "second answer" },
      dataset_name: "ds1",
      session_id: "sess-1",
    });

    expect(mock.requests.filter((r) => r.path === "/api/v1/remember/entry").length).toBe(1);
    const rememberRequests = mock.requests.filter((r) => r.path === "/api/v1/remember");
    expect(rememberRequests.length).toBe(2);
    // The QA entry serialized as text (client.ts's local formatQAEntryAsText).
    expect(rememberRequests[0]!.body).toContain("Q: What does the user prefer?");
    expect(rememberRequests[0]!.body).toContain("A: Dark mode.");

    expect(getCapabilities(mock.url).rememberEntrySupported).toBe(false);
  });

  it("uses /remember/entry directly, and never calls /remember, when the route works", async () => {
    mock = await startMockCognee();
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    await client.rememberEntry({
      entry: { type: "qa", question: "q", answer: "a" },
      dataset_name: "ds1",
    });

    expect(mock.requests.filter((r) => r.path === "/api/v1/remember/entry").length).toBe(1);
    expect(mock.requests.some((r) => r.path === "/api/v1/remember")).toBe(false);
    expect(getCapabilities(mock.url).rememberEntrySupported).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Multipart body shape (remember/add are multipart, not JSON)
// ---------------------------------------------------------------------------

describe("multipart body shape", () => {
  it("remember() sends a multipart/form-data body with every field as its own part", async () => {
    mock = await startMockCognee();
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    await client.remember({
      raw_data: ["fact one", "fact two"],
      datasetName: "ds1",
      session_id: "sess-1",
      node_set: ["resource:r1", "thread:t1"],
      run_in_background: true,
    });

    const req = mock.requests.find((r) => r.path === "/api/v1/remember")!;
    expect(req.headers["content-type"]).toContain("multipart/form-data");

    // Every raw_data item is its own `name="raw_data"` part (not a joined/CSV field).
    expect(req.body.match(/name="raw_data"/g)?.length).toBe(2);
    expect(req.body).toContain("fact one");
    expect(req.body).toContain("fact two");
    expect(req.body).toContain('name="datasetName"');
    expect(req.body).toContain("ds1");
    expect(req.body).toContain('name="session_id"');
    expect(req.body).toContain("sess-1");
    expect(req.body.match(/name="node_set"/g)?.length).toBe(2);
    expect(req.body).toContain("resource:r1");
    expect(req.body).toContain("thread:t1");
    // content_type must never be sent — a live cognee 1.5.4 rejects raw_data
    // for any content_type other than 'code'; plain text is the server default.
    expect(req.body).not.toContain('name="content_type"');
    expect(req.body).toContain('name="run_in_background"');
    expect(req.body).toContain("true");
    // datasetId was never set — its part must be entirely absent, not sent empty.
    expect(req.body).not.toContain('name="datasetId"');
  });

  it("add() sends the same multipart shape minus session_id (add() has no session concept)", async () => {
    mock = await startMockCognee();
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    await client.add({ raw_data: ["fact"], datasetId: DATASET_ID, node_set: ["resource:r1"], run_in_background: false });

    const req = mock.requests.find((r) => r.path === "/api/v1/add")!;
    expect(req.headers["content-type"]).toContain("multipart/form-data");
    expect(req.body).toContain('name="raw_data"');
    expect(req.body).toContain('name="datasetId"');
    expect(req.body).toContain(DATASET_ID);
    expect(req.body).toContain('name="node_set"');
    expect(req.body).not.toContain('name="session_id"');
  });
});

// ---------------------------------------------------------------------------
// Dataset id cache (name -> id, in memory, for process lifetime)
// ---------------------------------------------------------------------------

describe("ensureDataset() dataset-id cache", () => {
  it("calls POST /api/v1/datasets exactly once for N sequential calls with the same name", async () => {
    mock = await startMockCognee();
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    const first = await client.ensureDataset(DATASET_NAME);
    const second = await client.ensureDataset(DATASET_NAME);
    const third = await client.ensureDataset(DATASET_NAME);

    expect(first).toEqual(second);
    expect(second).toEqual(third);
    expect(mock.requests.filter((r) => r.path === "/api/v1/datasets" && r.method === "POST").length).toBe(1);
  });

  it("collapses N concurrent calls with the same name onto a single in-flight request", async () => {
    mock = await startMockCognee({ latencyMs: 20 }); // latency wide enough that the race is meaningful.
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    const [a, b, c] = await Promise.all([
      client.ensureDataset("concurrent-ds"),
      client.ensureDataset("concurrent-ds"),
      client.ensureDataset("concurrent-ds"),
    ]);

    expect(a).toEqual(b);
    expect(b).toEqual(c);
    expect(mock.requests.filter((r) => r.path === "/api/v1/datasets" && r.method === "POST").length).toBe(1);
  });

  it("calls POST /api/v1/datasets once PER DISTINCT name", async () => {
    mock = await startMockCognee();
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    await client.ensureDataset("ds-a");
    await client.ensureDataset("ds-b");
    await client.ensureDataset("ds-a"); // repeat — must not add a 3rd call.

    expect(mock.requests.filter((r) => r.path === "/api/v1/datasets" && r.method === "POST").length).toBe(2);
  });

  it("the cache is per-client-instance, not global — a second client re-fetches", async () => {
    mock = await startMockCognee();
    const clientA = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });
    const clientB = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    await clientA.ensureDataset("shared-name");
    await clientB.ensureDataset("shared-name");

    expect(mock.requests.filter((r) => r.path === "/api/v1/datasets" && r.method === "POST").length).toBe(2);
  });
});
