import { CogneeHttpClient } from "../src/client";

// ---------------------------------------------------------------------------
// Circuit breaker + per-op timeout tests.
//
// The breaker is exercised through the public fetchAPI transport (the single
// chokepoint every operation flows through). Only genuine backend trouble
// trips it: UNREACHABLE (connection failure / timeout) or a 5xx. A reachable
// 4xx (401/403) is surfaced but must never trip.
// ---------------------------------------------------------------------------

const THRESHOLD = 3;
const COOLDOWN_MS = 1_000;

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

// A minimal Response stand-in: the client only reads ok/status and either
// .json() (2xx) or .text() (non-ok).
function httpResponse(status: number, body: unknown = {}): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
  } as unknown as Response;
}

// Build a client with fast breaker settings and an apiKey so ensureAuth()
// short-circuits without a login round-trip. Positional args mirror the
// constructor: baseUrl, apiKey, username, password, requestTimeout,
// ingestionTimeout, mode, recallTimeout, breakerEnabled, threshold, cooldown.
function makeClient(overrides: { recallTimeoutMs?: number; breakerEnabled?: boolean } = {}): CogneeHttpClient {
  return new CogneeHttpClient(
    "http://test",
    "key",
    "",
    "",
    60_000,
    300_000,
    "local",
    overrides.recallTimeoutMs ?? 60_000,
    overrides.breakerEnabled ?? true,
    THRESHOLD,
    COOLDOWN_MS,
  );
}

// One transport call with retries disabled, so a failure is a single fetch.
function call(client: CogneeHttpClient, timeoutMs = 60_000): Promise<unknown> {
  return client.fetchAPI("/api/v1/x", { method: "GET" }, timeoutMs, undefined, 0);
}

describe("circuit breaker", () => {
  it("trips after repeated UNREACHABLE responses", async () => {
    const client = makeClient();
    mockFetch.mockRejectedValue(new TypeError("connect ECONNREFUSED"));

    for (let i = 0; i < THRESHOLD; i++) {
      await expect(call(client)).rejects.toThrow(/ECONNREFUSED/);
    }
    expect(mockFetch).toHaveBeenCalledTimes(THRESHOLD);

    // Breaker is now open: the next call short-circuits without hitting fetch.
    await expect(call(client)).rejects.toThrow(/circuit open/);
    expect(mockFetch).toHaveBeenCalledTimes(THRESHOLD);
  });

  it("trips after repeated 5xx responses", async () => {
    const client = makeClient();
    mockFetch.mockResolvedValue(httpResponse(503, "service unavailable"));

    for (let i = 0; i < THRESHOLD; i++) {
      await expect(call(client)).rejects.toThrow(/\(503\)/);
    }
    expect(mockFetch).toHaveBeenCalledTimes(THRESHOLD);

    await expect(call(client)).rejects.toThrow(/circuit open/);
    expect(mockFetch).toHaveBeenCalledTimes(THRESHOLD);
  });

  it("does not trip on 401 auth failures", async () => {
    const client = makeClient();
    mockFetch.mockResolvedValue(httpResponse(401, "unauthorized"));

    const attempts = THRESHOLD + 2;
    for (let i = 0; i < attempts; i++) {
      await expect(call(client)).rejects.toThrow(/\(401\)/);
    }
    // Every attempt reached the server — the breaker never opened.
    expect(mockFetch).toHaveBeenCalledTimes(attempts);
  });

  it("does not trip on 403 auth failures", async () => {
    const client = makeClient();
    mockFetch.mockResolvedValue(httpResponse(403, "forbidden"));

    const attempts = THRESHOLD + 2;
    for (let i = 0; i < attempts; i++) {
      await expect(call(client)).rejects.toThrow(/\(403\)/);
    }
    expect(mockFetch).toHaveBeenCalledTimes(attempts);
  });

  it("resets the failure count after a success", async () => {
    const client = makeClient();
    const unreachable = new TypeError("unreachable");
    mockFetch
      .mockRejectedValueOnce(unreachable)
      .mockRejectedValueOnce(unreachable)
      .mockResolvedValueOnce(httpResponse(200, { ok: true }))
      .mockRejectedValueOnce(unreachable)
      .mockRejectedValueOnce(unreachable);

    await expect(call(client)).rejects.toThrow(); // 1 failure
    await expect(call(client)).rejects.toThrow(); // 2 failures
    await expect(call(client)).resolves.toEqual({ ok: true }); // reset to 0
    await expect(call(client)).rejects.toThrow(); // 1 failure
    await expect(call(client)).rejects.toThrow(); // 2 failures — still below threshold

    // Breaker never opened, so the next call still reaches the server.
    mockFetch.mockResolvedValueOnce(httpResponse(200, {}));
    await expect(call(client)).resolves.toEqual({});
    expect(mockFetch).toHaveBeenCalledTimes(6);
  });

  it("reopens for a probe after the cooldown elapses", async () => {
    const client = makeClient();
    let now = 10_000;
    jest.spyOn(Date, "now").mockImplementation(() => now);
    mockFetch.mockRejectedValue(new TypeError("unreachable"));

    for (let i = 0; i < THRESHOLD; i++) {
      await expect(call(client)).rejects.toThrow();
    }
    // Open until now + cooldown; a call inside the window short-circuits.
    await expect(call(client)).rejects.toThrow(/circuit open/);
    expect(mockFetch).toHaveBeenCalledTimes(THRESHOLD);

    // Advance past the cooldown → half-open: the next call probes the server.
    now += COOLDOWN_MS + 1;
    mockFetch.mockResolvedValueOnce(httpResponse(200, {}));
    await expect(call(client)).resolves.toEqual({});
    expect(mockFetch).toHaveBeenCalledTimes(THRESHOLD + 1);
  });

  it("does not reopen immediately when the post-cooldown probe fails", async () => {
    // Mirrors the Python reference (_is_breaker_open): the half-open transition
    // resets the counter, so a failing probe starts a fresh streak rather than
    // reopening after a single failure.
    const client = makeClient();
    let now = 10_000;
    jest.spyOn(Date, "now").mockImplementation(() => now);
    mockFetch.mockRejectedValue(new TypeError("unreachable"));

    for (let i = 0; i < THRESHOLD; i++) {
      await expect(call(client)).rejects.toThrow();
    }
    await expect(call(client)).rejects.toThrow(/circuit open/);
    expect(mockFetch).toHaveBeenCalledTimes(THRESHOLD);

    // Cooldown elapses → the probe and the call after it both reach the server
    // (counter reset to 0, then 1 — still below the threshold).
    now += COOLDOWN_MS + 1;
    await expect(call(client)).rejects.toThrow(/unreachable/);
    expect(mockFetch).toHaveBeenCalledTimes(THRESHOLD + 1);
    await expect(call(client)).rejects.toThrow(/unreachable/);
    expect(mockFetch).toHaveBeenCalledTimes(THRESHOLD + 2);
  });

  it("stays closed when disabled", async () => {
    const client = makeClient({ breakerEnabled: false });
    mockFetch.mockRejectedValue(new TypeError("unreachable"));

    const attempts = THRESHOLD + 3;
    for (let i = 0; i < attempts; i++) {
      await expect(call(client)).rejects.toThrow(/unreachable/);
    }
    // No short-circuit — every call hit the server.
    expect(mockFetch).toHaveBeenCalledTimes(attempts);
  });
});

describe("per-op timeout", () => {
  it("recall forwards the configured recallTimeoutMs to the transport", async () => {
    // requestTimeoutMs is 60s; recall gets a tighter 5s bound.
    const client = makeClient({ recallTimeoutMs: 5_000 });
    const spy = jest.spyOn(client, "fetchAPI").mockResolvedValue([]);

    await client.recall({ queryText: "q", searchPrompt: "", searchType: "GRAPH_COMPLETION", datasetIds: ["d1"] });

    expect(spy).toHaveBeenCalledWith("/api/v1/recall", expect.any(Object), 5_000);
  });

  it("aborts a request once the per-op timeout elapses", async () => {
    jest.useFakeTimers();
    try {
      const client = makeClient();
      let signal: AbortSignal | undefined;
      mockFetch.mockImplementation((_url: string, init: RequestInit) => {
        signal = init.signal as AbortSignal;
        return new Promise<Response>((_resolve, reject) => {
          signal!.addEventListener("abort", () =>
            reject(new DOMException("The operation was aborted.", "AbortError")));
        });
      });

      const pending = call(client, 5_000);
      await Promise.resolve(); // let fetch get invoked
      expect(signal!.aborted).toBe(false);

      jest.advanceTimersByTime(5_000);
      await expect(pending).rejects.toThrow(/aborted/i);
      expect(signal!.aborted).toBe(true);
    } finally {
      jest.useRealTimers();
    }
  });
});
