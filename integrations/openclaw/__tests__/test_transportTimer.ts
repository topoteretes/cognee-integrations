import { CogneeHttpClient } from "../src/client";

// ---------------------------------------------------------------------------
// Transport abort-timer hygiene.
//
// fetchAPI arms a per-request abort timer (setTimeout -> controller.abort()) to
// bound each call. On the success path the timer must be cleared: a leaked,
// still-armed timer keeps the Node event loop alive and delays process exit up
// to timeoutMs. Only the error and 401-relogin paths cleared it before; this
// pins the success path too.
// ---------------------------------------------------------------------------

let mockFetch: jest.Mock;
let originalFetch: typeof globalThis.fetch;

beforeEach(() => {
  originalFetch = globalThis.fetch;
  mockFetch = jest.fn();
  (globalThis as unknown as { fetch: unknown }).fetch = mockFetch;
});

afterEach(() => {
  (globalThis as unknown as { fetch: unknown }).fetch = originalFetch;
  jest.restoreAllMocks();
});

// A minimal 2xx Response stand-in: fetchAPI only reads ok/status and .json().
function httpOk(body: unknown = {}): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

// apiKey is set so ensureAuth() short-circuits without a login round-trip.
function makeClient(): CogneeHttpClient {
  return new CogneeHttpClient("http://test", "key");
}

describe("fetchAPI abort timer", () => {
  it("clears the per-request timeout on the success path", async () => {
    jest.useFakeTimers();
    try {
      const client = makeClient();
      let signal: AbortSignal | undefined;
      mockFetch.mockImplementation((_url: string, init: RequestInit) => {
        signal = init.signal as AbortSignal;
        return Promise.resolve(httpOk({ ok: true }));
      });

      // retries disabled so a single fetch settles the call.
      await client.fetchAPI("/api/v1/x", { method: "GET" }, 5_000, undefined, 0);

      // Timer was cleared: nothing left pending, and advancing well past the
      // timeout never aborts the already-settled request.
      expect(jest.getTimerCount()).toBe(0);
      jest.advanceTimersByTime(60_000);
      expect(signal?.aborted).toBe(false);
    } finally {
      jest.useRealTimers();
    }
  });
});
