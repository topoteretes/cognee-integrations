import { CogneeHttpClient } from "../src/client";
import type { CogneeSearchType } from "../src/types";

// Cold-start warmup + first-recall retry (#3546).
//
// These tests exercise the REAL client (recall / fetchAPI / fireWarmupPing), so
// they mock the network by overwriting global.fetch rather than mocking the
// class. jest isn't run in CI (tsc-only) — verified locally + screenshot.

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

describe("recall first-recall cold-start retry (#3546)", () => {
  it("retries a real 504 on the first recall, then succeeds on 200", async () => {
    process.env.COGNEE_RECALL_RETRIES = "1";
    const fetchMock = jest
      .fn()
      .mockResolvedValueOnce(errResp(504, "gateway timeout")) // fetchAPI throws `(504)`
      .mockResolvedValueOnce(jsonResp([])); // retry succeeds
    setFetch(fetchMock as unknown as typeof fetch);

    const client = cloudClient();
    const results = await client.recall(recallParams({ firstRecall: true }));

    expect(results).toEqual([]);
    expect(fetchMock).toHaveBeenCalledTimes(2); // 1 cold miss + 1 retry — the case #127 can't pass
  });

  it("does NOT retry a 400 on the first recall (non-retryable)", async () => {
    process.env.COGNEE_RECALL_RETRIES = "1";
    const fetchMock = jest.fn().mockResolvedValue(errResp(400, "bad request"));
    setFetch(fetchMock as unknown as typeof fetch);

    const client = cloudClient();
    await expect(client.recall(recallParams({ firstRecall: true }))).rejects.toThrow();

    expect(fetchMock).toHaveBeenCalledTimes(1); // 4xx aborts immediately, no retry
  });

  it("does NOT engage the wrapper on a non-first recall (generic path unchanged)", async () => {
    // firstRecall:false routes through the default fetchAPI, whose generic retry
    // only fires on timeouts — a 504 is thrown immediately, never retried.
    process.env.COGNEE_RECALL_RETRIES = "1";
    const fetchMock = jest.fn().mockResolvedValue(errResp(504, "gateway timeout"));
    setFetch(fetchMock as unknown as typeof fetch);

    const client = cloudClient();
    await expect(client.recall(recallParams({ firstRecall: false }))).rejects.toThrow();

    expect(fetchMock).toHaveBeenCalledTimes(1); // no cold-start retry for non-first recalls
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
    const results = await client.recall(recallParams({ firstRecall: false }));
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
