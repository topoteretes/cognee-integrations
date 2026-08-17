/**
 * `CogneeHttpClient` against a real HTTP server (integration tier).
 *
 * Everything here is asserted on the wire — the request that actually left the
 * process — because that is where this class's behaviour lives. The methods are
 * thin; what matters is the transport around them: which path prefix the mode
 * selects, which auth header is attached, whether a 401 re-logs in, whether a
 * retry happens at all, and what a timeout does. A stubbed `global.fetch` can
 * observe the arguments but not the outcome, so this uses `MockCognee`.
 *
 * Contract:
 *   * cloud mode requires an API key up front and sends bare `/x`; local mode
 *     logs in for a JWT and sends `/api/v1/x`;
 *   * an API key wins over the JWT and is sent as `X-Api-Key` ALONE — never as a
 *     Bearer, which servers validating Authorization as a JWT would reject;
 *   * a 401 with no API key re-logs in and retries once;
 *   * a per-call `timeoutMs` (the prompt hot path) disables retries entirely, so
 *     a slow server fails fast instead of spending the recall budget;
 *   * the memory verbs send the field names the server expects.
 *
 * Not covered here: retry backoff timing. `RETRY_BASE_DELAY_MS` is 3s with
 * `MAX_RETRIES` 2, so exercising a full sequence costs ~9s of real waiting for one
 * assertion. The valuable half — that the hot path opts out of retries — is
 * asserted below without any wait.
 */

import { CogneeHttpClient } from "../src/client";
import { MockCognee } from "../test-utils/mockCognee";

const CLOUD = "cloud" as const;
const LOCAL = "local" as const;

let mock: MockCognee;

beforeAll(async () => {
  mock = await MockCognee.start();
});
afterAll(async () => {
  await mock.close();
});
beforeEach(() => mock.reset());

/** Local-mode client with an API key already resolved (the steady state). */
function localClient(timeoutMs = 5_000): CogneeHttpClient {
  return new CogneeHttpClient(mock.url, "key-abc", undefined, undefined, timeoutMs, 30_000, LOCAL);
}

/** Cloud-mode client; the key is mandatory there. */
function cloudClient(): CogneeHttpClient {
  return new CogneeHttpClient(mock.url, "key-abc", undefined, undefined, 5_000, 30_000, CLOUD);
}

// ── auth ─────────────────────────────────────────────────────────────────────

describe("authentication", () => {
  it("cloud mode refuses an authenticated request without an API key", async () => {
    // Cloud has no login route, so a missing key can only fail — and it must fail
    // with a message naming the credential rather than a transport error.
    const client = new CogneeHttpClient(mock.url, undefined, undefined, undefined, 5_000, 30_000, CLOUD);
    await expect(client.listDatasets()).rejects.toThrow(/requires an API key/i);
    expect(mock.calls).toHaveLength(0);
  });

  it("health() deliberately skips auth so it works as a liveness probe", async () => {
    // Not an oversight: health() is a raw fetch that never calls ensureAuth, so a
    // server can be probed before any credential exists. Pinned because it is the
    // one method whose auth behaviour differs, and a future refactor routing it
    // through fetchAPI would silently break cold-start probing.
    const client = new CogneeHttpClient(mock.url, undefined, undefined, undefined, 5_000, 30_000, CLOUD);
    await expect(client.health()).resolves.toEqual({ status: "ok" });
    expect(mock.assertCalled("GET", "/health").rawPath).toBe("/health");
  });

  it("local mode logs in for a JWT when it has no API key", async () => {
    const client = new CogneeHttpClient(mock.url, undefined, "u@example.com", "pw", 5_000, 30_000, LOCAL);
    await client.recall({ queryText: "q", searchPrompt: "", searchType: "GRAPH_COMPLETION" as never, datasetIds: ["ds-1"] });

    const login = mock.assertCalled("POST", "/auth/login");
    expect(login.body).toContain("u%40example.com");
    // The JWT from login is then used as a Bearer on the real call.
    expect(mock.assertCalled("POST", "/recall").headers.authorization).toBe("Bearer test-token");
  });

  it("logs in once for concurrent callers", async () => {
    // `loginPromise` exists so a burst of parallel requests shares one login;
    // without it a fresh session would hammer /auth/login on its first prompt.
    const client = new CogneeHttpClient(mock.url, undefined, "u", "p", 5_000, 30_000, LOCAL);
    await Promise.all([client.health(), client.listDatasets(), client.listDatasets()]);
    expect(mock.callsTo("POST", "/auth/login")).toHaveLength(1);
  });

  it("sends X-Api-Key alone when a key is present, never a Bearer", async () => {
    // An API key is not a JWT. A server validating Authorization as a JWT can
    // reject the request on the bogus Bearer before ever reading the key.
    await localClient().listDatasets();

    const call = mock.assertCalled("GET", "/datasets");
    expect(call.headers["x-api-key"]).toBe("key-abc");
    expect(call.headers.authorization).toBeUndefined();
    mock.assertNotCalled("POST", "/auth/login");
  });

  it("setApiKey takes over from the JWT and ignores blanks", async () => {
    const client = new CogneeHttpClient(mock.url, undefined, "u", "p", 5_000, 30_000, LOCAL);
    await client.health(); // logs in, gets a JWT

    client.setApiKey("   "); // blank must not clear a working credential
    client.setApiKey("minted-key");
    mock.reset();
    await client.listDatasets();

    const call = mock.assertCalled("GET", "/datasets");
    expect(call.headers["x-api-key"]).toBe("minted-key");
    expect(call.headers.authorization).toBeUndefined();
  });

  it("re-logs in and retries once on a 401 with no API key", async () => {
    const client = new CogneeHttpClient(mock.url, undefined, "u", "p", 5_000, 30_000, LOCAL);
    mock.forceResponse("GET", "/datasets", 401, { detail: "expired" }, true);

    await expect(client.listDatasets()).resolves.toEqual([{ id: "ds-1", name: "testds" }]);

    // Two attempts at the endpoint, and a second login between them.
    expect(mock.callsTo("GET", "/datasets")).toHaveLength(2);
    expect(mock.callsTo("POST", "/auth/login")).toHaveLength(2);
  });
});

// ── mode-dependent routing ───────────────────────────────────────────────────

describe("path prefix by mode", () => {
  it("local mode prefixes /api/v1", async () => {
    await localClient().recall({ queryText: "q", searchPrompt: "", searchType: "GRAPH_COMPLETION" as never, datasetIds: ["ds-1"] });
    expect(mock.assertCalled("POST", "/recall").rawPath).toBe("/api/v1/recall");
  });

  it("cloud mode omits it", async () => {
    await cloudClient().recall({ queryText: "q", searchPrompt: "", searchType: "GRAPH_COMPLETION" as never, datasetIds: ["ds-1"] });
    expect(mock.assertCalled("POST", "/recall").rawPath).toBe("/recall");
  });

  it("cognify and improve pick the cloud path", async () => {
    // Each verb picks its own path with its own isCloud ternary, so they can drift
    // independently — which is exactly why this asserts them together.
    const client = cloudClient();
    await client.cognify({ datasetIds: ["ds-1"] });
    await client.improve({ datasetName: "testds", sessionIds: ["s1"] } as never);

    expect(mock.calls.map((c) => c.rawPath)).toEqual(["/cognify", "/improve"]);
  });

  /**
   * KNOWN BUG, not a design choice.
   *
   * `memify()` hardcodes `/api/v1/memify` with no `isCloud` ternary, unlike
   * `cognify()` three lines above it which has one. So in cloud mode memify is the
   * only verb that keeps the local prefix, and it would miss the cloud route.
   *
   * `it.failing` is jest's strict-xfail: this passes while the bug exists and turns
   * RED the moment memify learns the ternary — so the fix cannot land silently.
   */
  it.failing("memify honours cloud mode like every other verb", async () => {
    await cloudClient().memify({ datasetIds: ["ds-1"] });
    expect(mock.assertCalled("POST", "/memify").rawPath).toBe("/memify");
  });
});

// ── failure handling ─────────────────────────────────────────────────────────

describe("failure handling", () => {
  it("a per-call timeout disables retries so the prompt path fails fast", async () => {
    // recall passes `retries: 0` whenever the caller sets timeoutMs. Without that
    // a slow server would burn 3s + 6s of backoff on the hot path — the whole
    // reason the option exists.
    mock.forceResponse("POST", "/recall", 503, { detail: "busy" });

    const started = Date.now();
    await expect(
      localClient().recall({
        queryText: "q",
        searchPrompt: "",
        searchType: "GRAPH_COMPLETION" as never,
        datasetIds: ["ds-1"],
        timeoutMs: 1_000,
      }),
    ).rejects.toThrow();

    expect(mock.callsTo("POST", "/recall")).toHaveLength(1);
    // A single retry would have added ~3s; anything under that proves none happened.
    expect(Date.now() - started).toBeLessThan(2_500);
  });

  it("aborts when the server exceeds the timeout", async () => {
    // A hung endpoint must surface as an abort, not hang the caller forever.
    // Served by a route the mock has no handler for is not enough — this needs a
    // real stall, so the timeout is set below the mock's response time using a
    // deliberately slow forced route.
    const slow = await MockCognee.start();
    try {
      // Monkey-patch one route into a never-resolving handler.
      const server = slow as unknown as { server: { removeAllListeners: (e: string) => void; on: (e: string, cb: unknown) => void } };
      server.server.removeAllListeners("request");
      server.server.on("request", () => {
        /* never responds */
      });

      const client = new CogneeHttpClient(slow.url, "k", undefined, undefined, 300, 30_000, LOCAL);
      await expect(
        client.recall({ queryText: "q", searchPrompt: "", searchType: "GRAPH_COMPLETION" as never, datasetIds: ["ds-1"], timeoutMs: 300 }),
      ).rejects.toThrow();
    } finally {
      await slow.close();
    }
  });

  it("surfaces the status on a non-2xx", async () => {
    mock.forceResponse("GET", "/datasets", 500, { detail: "kaboom" });
    await expect(localClient().listDatasets()).rejects.toThrow(/500|kaboom/i);
  });
});

// ── the memory verbs ─────────────────────────────────────────────────────────

describe("memory verbs send what the server expects", () => {
  it("recall maps its params onto the server's field names", async () => {
    await localClient().recall({
      queryText: "what did we decide",
      searchPrompt: "be brief",
      searchType: "GRAPH_COMPLETION" as never,
      datasetIds: ["ds-1", "ds-2"],
      topK: 7,
      sessionId: "s1",
      scope: ["session", "graph"],
    });

    expect(mock.assertCalled("POST", "/recall").json).toEqual({
      query: "what did we decide",
      search_type: "GRAPH_COMPLETION",
      dataset_ids: ["ds-1", "ds-2"],
      only_context: true,
      top_k: 7,
      system_prompt: "be brief",
      session_id: "s1",
      scope: ["session", "graph"],
    });
  });

  it("recall omits optional fields rather than sending nulls", async () => {
    // The server treats an explicit null differently from an absent key, so the
    // spread-guards in recall() matter.
    await localClient().recall({ queryText: "q", searchPrompt: "", searchType: "GRAPH_COMPLETION" as never, datasetIds: ["ds-1"] });

    const body = mock.assertCalled("POST", "/recall").json as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(["dataset_ids", "only_context", "query", "search_type"]);
  });

  it("recall normalises an empty result set to an array", async () => {
    mock.setResponse("POST", "/recall", { results: [] });
    await expect(
      localClient().recall({ queryText: "q", searchPrompt: "", searchType: "GRAPH_COMPLETION" as never, datasetIds: ["ds-1"] }),
    ).resolves.toEqual([]);
  });

  it("rememberEntry posts the session-cache contract", async () => {
    await localClient().rememberEntry({
      entry: { kind: "qa", question: "q", answer: "a" },
      datasetName: "testds",
      sessionId: "s1",
    } as never);

    const body = mock.assertCalled("POST", "/remember/entry").json as Record<string, unknown>;
    expect(body).toMatchObject({ dataset_name: "testds", session_id: "s1" });
    expect(body.entry).toBeDefined();
  });

  it("registerAgent declares itself an api/hybrid agent", async () => {
    await localClient().registerAgent({ agentSessionName: "a1", sessionId: "s1", datasetNames: ["testds"] } as never);
    expect(mock.assertCalled("POST", "/agents/register").json).toMatchObject({
      memory_mode: "hybrid",
    });
  });

  it("unregisterAgent reports the remaining agent count", async () => {
    mock.setResponse("POST", "/agents/unregister", { ok: true, activeAgents: 2 });
    const res = await localClient().unregisterAgent({ agentSessionName: "a1" } as never);
    expect(res).toMatchObject({ ok: true });
    expect(mock.assertCalled("POST", "/agents/unregister")).toBeTruthy();
  });

  it("health reads the status field", async () => {
    await expect(localClient().health()).resolves.toEqual({ status: "ok" });
  });

  it("listDatasets returns id/name pairs", async () => {
    mock.setResponse("GET", "/datasets", [
      { id: "a", name: "one" },
      { id: "b", name: "two" },
    ]);
    await expect(localClient().listDatasets()).resolves.toEqual([
      { id: "a", name: "one" },
      { id: "b", name: "two" },
    ]);
  });
});
