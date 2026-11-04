/**
 * `CogneeClient.recall()` — retry/backoff, per-call timeout opt-out, and the
 * `/recall` -> `/search` capability fallback.
 *
 * Retry/backoff tests run Jest's fake timers against a real `node:http` mock
 * server in-process, since real backoff waits (3s/6s/12s) would otherwise cost
 * real wall-clock time per test. Fake-timer scope stays narrow (setTimeout,
 * setInterval, Date only) so Node's own socket internals keep their own
 * microtask/immediate timers.
 */

import { jest } from "@jest/globals";
import { CogneeClient } from "../../src/client.js";
import { CogneeApiError } from "../../src/errors.js";
import { getCapabilities, resetCapabilities } from "../../src/capabilities.js";
import { startMockCognee, MOCK_API_KEY, type MockCognee } from "../test-utils/mock-cognee.js";
import { RECALL_RESPONSE, SEARCH_RESPONSE } from "../test-utils/fixtures.js";

let mock: MockCognee;

afterEach(async () => {
  if (mock) await mock.close();
  // A test that throws before its own jest.useRealTimers() must not leak
  // fake timers into later tests in this file (each test file gets a fresh
  // worker, but not necessarily fresh timer state within one file).
  jest.useRealTimers();
});

// ---------------------------------------------------------------------------
// Fake-timer harness: advance the virtual clock in small steps, real-awaiting
// between each step, until `promise` settles or a step budget is exhausted.
// This tolerates the (real, unavoidable) interleaving between the mock
// server's genuine async I/O and the virtual retry-delay clock — a single
// large `advanceTimersByTimeAsync` call can race a timer that gets scheduled
// mid-advance; stepping avoids relying on that ordering.
// ---------------------------------------------------------------------------

/**
 * Captured before any test installs fake timers, so this harness can hand
 * out a genuine (unfaked) real-time yield between virtual-clock advances —
 * see the loop below for why that turned out to be necessary.
 */
const realSetTimeout = globalThis.setTimeout;
function realDelay(ms: number): Promise<void> {
  return new Promise((resolve) => realSetTimeout(resolve, ms));
}

async function drainWithFakeTimers<T>(
  promise: Promise<T>,
  options: { stepMs?: number; maxSteps?: number } = {},
): Promise<T> {
  // advanceTimersByTimeAsync needs a generous budget, not the nominal delay:
  // real socket I/O interleaved with the faked timer can leave small advances
  // unsettled without erroring. 60 * 15_000ms costs only milliseconds of real
  // time and clears the ~21s (3s+6s+12s) worst case with wide margin.
  const stepMs = options.stepMs ?? 15_000;
  const maxSteps = options.maxSteps ?? 60;

  let settled = false;
  let outcome: { ok: true; value: T } | { ok: false; error: unknown } | undefined;
  promise.then(
    (value) => {
      settled = true;
      outcome = { ok: true, value };
    },
    (error: unknown) => {
      settled = true;
      outcome = { ok: false, error };
    },
  );

  for (let step = 0; step < maxSteps && !settled; step++) {
    // eslint-disable-next-line no-await-in-loop -- each step must observe the previous one's
    // effects.
    await jest.advanceTimersByTimeAsync(stepMs);
    // advanceTimersByTimeAsync alone doesn't always flush enough event-loop
    // turns for this in-process mock server's real TCP round trip; a real
    // 1ms yield here makes the drain reliable regardless of test ordering.
    // eslint-disable-next-line no-await-in-loop
    if (!settled) await realDelay(1);
  }

  if (!settled) {
    throw new Error(`drainWithFakeTimers: promise never settled after ${stepMs * maxSteps}ms of virtual time`);
  }
  if (outcome!.ok) return outcome!.value;
  throw outcome!.error;
}

function useNarrowFakeTimers(): void {
  // Only setTimeout/setInterval/Date are virtualized; the mock server is a
  // real http.Server sharing this process's event loop, so nextTick,
  // setImmediate, and queueMicrotask must stay real or Node's socket
  // internals could starve.
  jest.useFakeTimers({
    doNotFake: ["nextTick", "setImmediate", "clearImmediate", "queueMicrotask", "hrtime", "performance"],
  });
}

/**
 * Far larger than any virtual-time budget this file advances to: guarantees
 * `CogneeClient`'s own `requestTimeoutMs` `AbortController` timer never
 * fires mid-test off the advanced fake clock and aborts an attempt that
 * would otherwise have completed normally.
 */
const NO_PRACTICAL_TIMEOUT_MS = 10_000_000;

// ---------------------------------------------------------------------------
// Retry + exponential backoff (fake timers)
// ---------------------------------------------------------------------------

describe("retry with exponential backoff", () => {
  it("retries on 500 and succeeds once the server recovers, waiting 3s then 6s between attempts", async () => {
    mock = await startMockCognee({ failFirstN: 2 }); // first 2 requests of ANY route fail; the 3rd (2nd retry) succeeds.
    const client = new CogneeClient({
      baseUrl: mock.url,
      apiKey: MOCK_API_KEY,
      retries: 3,
      requestTimeoutMs: NO_PRACTICAL_TIMEOUT_MS,
    });

    useNarrowFakeTimers();
    const hits = await drainWithFakeTimers(client.recall({ query: "backoff success case" }));
    jest.useRealTimers();

    expect(hits.length).toBe(RECALL_RESPONSE.length);
    expect(hits[0]!.text).toBe(RECALL_RESPONSE[0]!.text);

    const recallRequests = mock.requests.filter((r) => r.path === "/api/v1/recall");
    expect(recallRequests.length).toBe(3); // 1 initial attempt + 2 retries.
  });

  it("gives up after exhausting retries (3 retries = 4 total attempts), waiting 3s/6s/12s between them", async () => {
    mock = await startMockCognee({ failFirstN: 100 }); // every request fails — retries never recover.
    const client = new CogneeClient({
      baseUrl: mock.url,
      apiKey: MOCK_API_KEY,
      retries: 3,
      requestTimeoutMs: NO_PRACTICAL_TIMEOUT_MS,
    });

    useNarrowFakeTimers();
    const rejection = drainWithFakeTimers(client.recall({ query: "backoff exhaustion case" })).catch(
      (error: unknown) => error,
    );
    const error = await rejection;
    jest.useRealTimers();

    expect(error).toMatchObject({ status: 500 });
    const recallRequests = mock.requests.filter((r) => r.path === "/api/v1/recall");
    expect(recallRequests.length).toBe(4); // 1 initial attempt + 3 retries, then give up.
  });

  it("honours a lower configured retries count", async () => {
    mock = await startMockCognee({ failFirstN: 100 });
    const client = new CogneeClient({
      baseUrl: mock.url,
      apiKey: MOCK_API_KEY,
      retries: 1,
      requestTimeoutMs: NO_PRACTICAL_TIMEOUT_MS,
    });

    useNarrowFakeTimers();
    const rejection = drainWithFakeTimers(client.recall({ query: "one retry only" })).catch((error: unknown) => error);
    const error = await rejection;
    jest.useRealTimers();

    expect(error).toBeInstanceOf(CogneeApiError);
    expect(mock.requests.filter((r) => r.path === "/api/v1/recall").length).toBe(2); // 1 initial + 1 retry.
  });
});

// ---------------------------------------------------------------------------
// Per-call timeoutMs disables retries entirely
// ---------------------------------------------------------------------------

describe("no retry when a per-call timeoutMs is set", () => {
  it("makes exactly one attempt and fails fast on a 500, even with client.retries > 0", async () => {
    mock = await startMockCognee({ failFirstN: 100 });
    // retries: 3 at the client level — proves it's the per-call `timeoutMs`,
    // not a client-wide setting, that switches retries off.
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY, retries: 3 });

    await expect(client.recall({ query: "no retry" }, { timeoutMs: 500 })).rejects.toMatchObject({ status: 500 });

    expect(mock.requests.filter((r) => r.path === "/api/v1/recall").length).toBe(1);
  });

  it("aborts on timeout when the server is slower than the per-call timeoutMs", async () => {
    mock = await startMockCognee({ latencyMs: 300 });
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    const before = Date.now();
    await expect(client.recall({ query: "will time out" }, { timeoutMs: 80 })).rejects.toMatchObject({
      status: 0,
      message: expect.stringMatching(/timed out/i),
    });
    const elapsed = Date.now() - before;

    // Real timers here (no fake-timer harness in this test) — the abort
    // should fire close to timeoutMs and must not have waited for the
    // server's full 300ms latency, let alone a retry's 3s backoff.
    expect(elapsed).toBeLessThan(300);
    expect(mock.requests.filter((r) => r.path === "/api/v1/recall").length).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// /recall -> /search capability fallback
// ---------------------------------------------------------------------------

describe("/recall -> /search fallback", () => {
  it("falls back to /search exactly once on a 404, then uses /search directly (never re-probes /recall)", async () => {
    mock = await startMockCognee({
      routes: { "POST /api/v1/recall": () => ({ status: 404, body: { detail: "no such route" } }) },
    });
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    const firstHits = await client.recall({ query: "first call — triggers the probe" });
    const secondHits = await client.recall({ query: "second call — should reuse the cached decision" });

    // Both calls succeed via the legacy /search route's response shape.
    expect(firstHits[0]!.text).toBe(SEARCH_RESPONSE[0]!.search_result);
    expect(secondHits[0]!.text).toBe(SEARCH_RESPONSE[0]!.search_result);

    const recallRequests = mock.requests.filter((r) => r.path === "/api/v1/recall");
    const searchRequests = mock.requests.filter((r) => r.path === "/api/v1/search");
    // Exactly one probe of /recall, ever — never re-attempted once the 404 is cached.
    expect(recallRequests.length).toBe(1);
    expect(searchRequests.length).toBe(2);

    expect(getCapabilities(mock.url).recallPath).toBe("/api/v1/search");
  });

  it("never falls back when /recall works — /search is never called", async () => {
    mock = await startMockCognee();
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY });

    await client.recall({ query: "recall works fine" });
    await client.recall({ query: "recall works fine again" });

    expect(mock.requests.filter((r) => r.path === "/api/v1/recall").length).toBe(2);
    expect(mock.requests.some((r) => r.path === "/api/v1/search")).toBe(false);
    expect(getCapabilities(mock.url).recallPath).toBe("/api/v1/recall");
  });

  it("a non-404 failure on /recall propagates without ever trying /search", async () => {
    resetCapabilities();
    mock = await startMockCognee({
      routes: { "POST /api/v1/recall": () => ({ status: 422, body: { detail: "bad query" } }) },
    });
    const client = new CogneeClient({ baseUrl: mock.url, apiKey: MOCK_API_KEY, retries: 0 });

    await expect(client.recall({ query: "malformed on purpose" })).rejects.toMatchObject({ status: 422 });

    expect(mock.requests.some((r) => r.path === "/api/v1/search")).toBe(false);
    // A non-404 error still proves the route exists — cached as the winner
    // per `resolveRecallPath`'s doc comment, so a later call doesn't pay a
    // needless fallback attempt either.
    expect(getCapabilities(mock.url).recallPath).toBe("/api/v1/recall");
  });
});
