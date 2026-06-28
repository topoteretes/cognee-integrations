import { CogneeHttpClient, CircuitBreaker } from "../src/client";

// ---------------------------------------------------------------------------
// Mock helpers
// ---------------------------------------------------------------------------

function mockFetch(status: number, body: unknown = {}) {
  global.fetch = jest.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as Response);
}

function mockFetchFail(errorName = "AbortError") {
  const err = new DOMException("timeout", errorName);
  global.fetch = jest.fn().mockRejectedValue(err);
}

// ---------------------------------------------------------------------------
// CircuitBreaker unit tests
// ---------------------------------------------------------------------------

describe("CircuitBreaker", () => {
  it("starts closed", () => {
    const b = new CircuitBreaker(3, 5000);
    expect(b.isOpen).toBe(false);
  });

  it("opens after threshold failures", () => {
    const b = new CircuitBreaker(3, 5000);
    b.recordFailure();
    b.recordFailure();
    expect(b.isOpen).toBe(false);
    b.recordFailure();
    expect(b.isOpen).toBe(true);
  });

  it("resets on success", () => {
    const b = new CircuitBreaker(2, 5000);
    b.recordFailure();
    b.recordFailure();
    expect(b.isOpen).toBe(true);
    b.recordSuccess();
    expect(b.isOpen).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// CogneeHttpClient breaker integration tests
// ---------------------------------------------------------------------------

describe("CogneeHttpClient breaker integration", () => {
  it("trips on repeated network errors, not on 401", async () => {
    const breaker = new CircuitBreaker(2, 60_000);
    const client = new CogneeHttpClient(
      "http://localhost:9999", "key", "", "", 1000, 1000, "cloud",
      1000, 1000, breaker,
    );

    // Two network failures (TypeError = network error, not retried as timeout)
    const networkErr = Object.assign(new Error("network failure"), { name: "TypeError" });
    global.fetch = jest.fn().mockRejectedValue(networkErr);
    await expect(client.recall({
      queryText: "q", searchPrompt: "", searchType: "GRAPH_COMPLETION",
      datasetIds: [], topK: 5,
    })).rejects.toThrow();
    await expect(client.recall({
      queryText: "q", searchPrompt: "", searchType: "GRAPH_COMPLETION",
      datasetIds: [], topK: 5,
    })).rejects.toThrow();

    expect(breaker.isOpen).toBe(true);

    // A 401 should NOT trip the breaker (auth config problem, not a transient outage)
    const b2 = new CircuitBreaker(3, 60_000);
    const c2 = new CogneeHttpClient(
      "http://localhost:9999", "key", "", "", 1000, 1000, "cloud",
      1000, 1000, b2,
    );
    mockFetch(401);
    await expect(c2.recall({
      queryText: "q", searchPrompt: "", searchType: "GRAPH_COMPLETION",
      datasetIds: [], topK: 5,
    })).rejects.toThrow();
    expect(b2.isOpen).toBe(false); // 401 must NOT trip the breaker
  });

  it("trips on 5xx", async () => {
    const breaker = new CircuitBreaker(1, 60_000);
    const client = new CogneeHttpClient(
      "http://localhost:9999", "key", "", "", 1000, 1000, "cloud",
      1000, 1000, breaker,
    );
    mockFetch(500, { error: "boom" });
    await expect(client.search({
      queryText: "q", searchPrompt: "", searchType: "GRAPH_COMPLETION",
      datasetIds: [], maxTokens: 512,
    })).rejects.toThrow();
    expect(breaker.isOpen).toBe(true);
  });

  it("rejects immediately when open, without calling fetch", async () => {
    const breaker = new CircuitBreaker(1, 60_000);
    breaker.recordFailure(); // trip it manually
    const client = new CogneeHttpClient(
      "http://localhost:9999", "key", "", "", 1000, 1000, "cloud",
      1000, 1000, breaker,
    );
    const spy = jest.fn();
    global.fetch = spy;
    await expect(client.search({
      queryText: "q", searchPrompt: "", searchType: "GRAPH_COMPLETION",
      datasetIds: [], maxTokens: 512,
    })).rejects.toThrow(/circuit open/);
    expect(spy).not.toHaveBeenCalled();
  });

  it("clears breaker on success", async () => {
    const breaker = new CircuitBreaker(3, 60_000);
    breaker.recordFailure();
    breaker.recordFailure();
    const client = new CogneeHttpClient(
      "http://localhost:9999", "key", "", "", 1000, 1000, "cloud",
      1000, 1000, breaker,
    );
    mockFetch(200, []);
    await client.search({
      queryText: "q", searchPrompt: "", searchType: "GRAPH_COMPLETION",
      datasetIds: [], maxTokens: 512,
    });
    expect(breaker.isOpen).toBe(false);
  });
});
