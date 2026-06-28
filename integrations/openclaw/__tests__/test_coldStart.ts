import { CogneeHttpClient } from "../src/client";

// Helper: replace global.fetch with a function
function setFetch(fn: typeof fetch) {
  global.fetch = fn as typeof global.fetch;
}

describe("fireWarmupPing", () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
  });
  afterEach(() => {
    process.env = originalEnv;
  });

  it("does not fire when COGNEE_WARMUP is unset", async () => {
    const spy = jest.fn().mockResolvedValue({ ok: true, json: async () => ({}) } as Response);
    setFetch(spy);
    const client = new CogneeHttpClient("https://cloud.cognee.ai", "key", "", "", 1000, 1000, "cloud");
    client.fireWarmupPing();
    await new Promise((r) => setTimeout(r, 20));
    expect(spy).not.toHaveBeenCalledWith(expect.stringContaining("/health"), expect.anything());
  });

  it("does not fire for localhost base URL even when enabled", async () => {
    process.env.COGNEE_WARMUP = "true";
    const spy = jest.fn().mockResolvedValue({ ok: true } as Response);
    setFetch(spy);
    const client = new CogneeHttpClient("http://localhost:8000", "key", "", "", 1000, 1000, "local");
    client.fireWarmupPing();
    await new Promise((r) => setTimeout(r, 20));
    expect(spy).not.toHaveBeenCalled();
  });

  it("fires GET /health in background when COGNEE_WARMUP=true and remote URL", async () => {
    process.env.COGNEE_WARMUP = "true";
    let called = false;
    setFetch(async (url) => {
      if (String(url).includes("/health")) called = true;
      return { ok: true } as Response;
    });
    const client = new CogneeHttpClient("https://cloud.cognee.ai", "key", "", "", 1000, 1000, "cloud");
    client.fireWarmupPing();
    await new Promise((r) => setTimeout(r, 50));
    expect(called).toBe(true);
  });

  it("swallows errors silently", async () => {
    process.env.COGNEE_WARMUP = "true";
    setFetch(async () => { throw new Error("network down"); });
    const client = new CogneeHttpClient("https://cloud.cognee.ai", "key", "", "", 1000, 1000, "cloud");
    expect(() => client.fireWarmupPing()).not.toThrow();
    await new Promise((r) => setTimeout(r, 50)); // give the void promise time to reject
  });
});

describe("recall firstRecall retry", () => {
  it("retries on timeout when firstRecall=true and succeeds on 2nd attempt", async () => {
    let calls = 0;
    setFetch(async () => {
      calls++;
      if (calls === 1) throw new DOMException("timeout", "AbortError");
      return { ok: true, json: async () => [] } as unknown as Response;
    });
    process.env.COGNEE_RECALL_RETRIES = "2";
    process.env.COGNEE_RECALL_BACKOFF_MS = "0";  // no sleep in tests
    const client = new CogneeHttpClient("https://cloud.cognee.ai", "key", "", "", 2000, 2000, "cloud");
    const results = await client.recall({
      queryText: "test", searchPrompt: "", searchType: "GRAPH_COMPLETION",
      datasetIds: [], firstRecall: true,
    });
    expect(results).toEqual([]);
    expect(calls).toBe(2);
  });

  it("does NOT retry when firstRecall is false", async () => {
    let calls = 0;
    setFetch(async () => {
      calls++;
      throw new DOMException("timeout", "AbortError");
    });
    process.env.COGNEE_RECALL_RETRIES = "2";
    const client = new CogneeHttpClient("https://cloud.cognee.ai", "key", "", "", 500, 500, "cloud");
    await expect(
      client.recall({
        queryText: "test", searchPrompt: "", searchType: "GRAPH_COMPLETION",
        datasetIds: [], firstRecall: false,
      })
    ).rejects.toThrow();
    expect(calls).toBe(1); // zero retries
  });

  it("degrades gracefully after exhausting retries", async () => {
    setFetch(async () => { throw new DOMException("timeout", "AbortError"); });
    process.env.COGNEE_RECALL_RETRIES = "1";
    process.env.COGNEE_RECALL_BACKOFF_MS = "0";
    const client = new CogneeHttpClient("https://cloud.cognee.ai", "key", "", "", 500, 500, "cloud");
    await expect(
      client.recall({
        queryText: "test", searchPrompt: "", searchType: "GRAPH_COMPLETION",
        datasetIds: [], firstRecall: true,
      })
    ).rejects.toThrow(); // throws, caller catches and falls back to no-context
  });
});
