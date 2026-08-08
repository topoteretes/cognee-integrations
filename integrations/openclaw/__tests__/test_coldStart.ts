import { CogneeHttpClient } from "../src/client";
import type { CogneeSearchType } from "../src/types";
import { withColdStartRetry, isColdStartRetryable } from "../src/plugin";

// Cold-start warmup + first-recall retry (#3546).
//
// Warmup tests exercise the REAL client (recall / fetchAPI / fireWarmupPing), so
// they mock the network by overwriting global.fetch. Retry tests exercise the
// exported wrappers from plugin.ts directly.

const ORIG_ENV = process.env;
const REAL_FETCH = global.fetch;

// Let a fire-and-forget async IIFE (fireWarmupPing) settle before asserting.
const flush = () => new Promise((r) => setTimeout(r, 0));

function jsonResp(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response;
}

function errResp(status: number, text: string): Response {
  return { ok: false, status, text: async () => text } as unknown as Response;
}

function setFetch(fn: typeof fetch): void {
  global.fetch = fn as typeof global.fetch;
}

function cloudClient(baseUrl = "https://cloud.cognee.ai"): CogneeHttpClient {
  // Short timeoutMs: fetchAPI doesn't clear its abort timer on the success path,
  // so a 200 mock leaves a pending timer; a small value lets it fire inside
  // jest's exit window instead of tripping the open-handle warning.
  return new CogneeHttpClient(baseUrl, "key", "", "", 200, 200, "cloud");
}

function recallParams(over: Partial<Parameters<CogneeHttpClient["recall"]>[0]> = {}) {
  return {
    queryText: "test",
    searchPrompt: "",
    searchType: "GRAPH_COMPLETION" as CogneeSearchType,
    datasetIds: [] as string[],
    ...over,
  };
}

beforeEach(() => {
  // Snapshot env per-test so COGNEE_* mutations never leak across tests.
  process.env = { ...ORIG_ENV };
  // Retries must not actually sleep — keep tests fast and deterministic.
  process.env.COGNEE_RECALL_BACKOFF_MS = "0";
});

afterEach(() => {
  process.env = ORIG_ENV;
  global.fetch = REAL_FETCH;
  jest.clearAllMocks();
});

describe("isColdStartRetryable", () => {
  it("returns true for an Error with name AbortError", () => {
    const err = new Error("aborted");
    err.name = "AbortError";
    expect(isColdStartRetryable(err)).toBe(true);
  });

  it("returns true for a DOMException with name AbortError", () => {
    const err = new DOMException("aborted", "AbortError");
    expect(isColdStartRetryable(err)).toBe(true);
  });

  it("returns true for an Error matching Cognee request failed (504)", () => {
    const err = new Error("Cognee request failed (504): Gateway Timeout");
    expect(isColdStartRetryable(err)).toBe(true);
  });

  it("returns false for a 400-style Error message", () => {
    const err = new Error("Cognee request failed (400): Bad Request");
    expect(isColdStartRetryable(err)).toBe(false);
  });

  it("returns false for a generic Error unrelated to either pattern", () => {
    const err = new Error("network down");
    expect(isColdStartRetryable(err)).toBe(false);
  });
});

describe("withColdStartRetry", () => {
  it("retries a real 504 on first recall, then succeeds", async () => {
    process.env.COGNEE_RECALL_RETRIES = "1";
    const fn = jest.fn()
      .mockRejectedValueOnce(new Error("Cognee request failed (504)"))
      .mockResolvedValueOnce("success");

    const result = await withColdStartRetry(true, fn);
    expect(result).toBe("success");
    expect(fn).toHaveBeenCalledTimes(2);
  });

  it("does NOT retry a 400 (non-retryable)", async () => {
    process.env.COGNEE_RECALL_RETRIES = "1";
    const fn = jest.fn().mockRejectedValueOnce(new Error("Cognee request failed (400)"));

    await expect(withColdStartRetry(true, fn)).rejects.toThrow("Cognee request failed (400)");
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("does NOT engage retry when isFirst is false", async () => {
    process.env.COGNEE_RECALL_RETRIES = "1";
    const fn = jest.fn().mockRejectedValueOnce(new Error("Cognee request failed (504)"));

    await expect(withColdStartRetry(false, fn)).rejects.toThrow("Cognee request failed (504)");
    expect(fn).toHaveBeenCalledTimes(1); // Short-circuits
  });

  it("respects COGNEE_RECALL_RETRIES=0 -> no retry even if isFirst and retryable", async () => {
    process.env.COGNEE_RECALL_RETRIES = "0";
    const fn = jest.fn().mockRejectedValueOnce(new Error("Cognee request failed (504)"));

    await expect(withColdStartRetry(true, fn)).rejects.toThrow("Cognee request failed (504)");
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("backoff never fires immediately (jitter check)", async () => {
    process.env.COGNEE_RECALL_RETRIES = "1";
    process.env.COGNEE_RECALL_BACKOFF_MS = "0";
    const fn = jest.fn()
      .mockRejectedValueOnce(new Error("Cognee request failed (504)"))
      .mockResolvedValueOnce("success");

    const start = Date.now();
    const result = await withColdStartRetry(true, fn);
    const end = Date.now();

    expect(result).toBe("success");
    expect(fn).toHaveBeenCalledTimes(2);
    // With backoff=0, it should proceed essentially instantly (with minimal jitter).
    expect(end - start).toBeLessThan(50);
  });
});

describe("fireWarmupPing (#3546)", () => {
  it("does NOT fire when COGNEE_WARMUP is unset", async () => {
    delete process.env.COGNEE_WARMUP;
    const fetchMock = jest.fn().mockResolvedValue(jsonResp({}));
    setFetch(fetchMock as unknown as typeof fetch);

    const client = cloudClient();
    client.fireWarmupPing();
    await flush();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("swallows a warmup failure and never affects a later recall", async () => {
    process.env.COGNEE_WARMUP = "true";
    // /health rejects (cold/unreachable); everything else resolves 200.
    const fetchMock = jest.fn(async (url: unknown) => {
      if (String(url).includes("/health")) throw new Error("network down");
      return jsonResp([]);
    });
    setFetch(fetchMock as unknown as typeof fetch);

    const client = cloudClient();
    expect(() => client.fireWarmupPing()).not.toThrow(); // returns void, never throws
    await flush(); // let the fire-and-forget IIFE reject + get swallowed

    // A subsequent recall still works despite the failed warmup.
    const results = await client.recall(recallParams());
    expect(results).toEqual([]);
  });

  it("skips warmup for a localhost base URL even when enabled", async () => {
    process.env.COGNEE_WARMUP = "true";
    const fetchMock = jest.fn().mockResolvedValue(jsonResp({}));
    setFetch(fetchMock as unknown as typeof fetch);

    const client = cloudClient("http://localhost:8000");
    client.fireWarmupPing();
    await flush();

    expect(fetchMock).not.toHaveBeenCalled(); // cold start is a cloud problem
  });
});
