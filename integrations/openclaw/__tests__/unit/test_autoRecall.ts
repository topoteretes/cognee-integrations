/**
 * Auto recall (SDK-1393): graph recall that names no dataset, routed by a
 * cognee >= 1.6.6 server to the best-matching dataset(s). Covers the version
 * gate on `/health`, the request shape, and memory_search's single routed
 * request. The per-conversation recall mode lives in test_datasetSwitch.ts.
 */

import { CogneeHttpClient, versionAtLeast, versionTuple, DATASET_ROUTING_MIN_VERSION } from "../../src/client";
import { resolveConfig } from "../../src/config";
import { createMemoryTools, type MemorySearchResult, type MemoryToolsDeps } from "../../src/tools";

describe("dataset routing version gate", () => {
  it("parses the leading release of a version", () => {
    expect(versionTuple("1.6.6")).toEqual([1, 6, 6]);
    expect(versionTuple("1.6.6rc1")).toEqual([1, 6, 6]);
    expect(versionTuple("1.6.6.rc1")).toEqual([1, 6, 6]);
    expect(versionTuple("v1.7.0.dev0")).toEqual([1, 7, 0]);
    expect(versionTuple("")).toEqual([]);
    expect(versionTuple("unknown")).toEqual([]);
  });

  // A release and its build labels count as the release; any other suffix
  // (pre-release, dev build, unknown label) only above it (cognee 1.6.6rc1
  // reports "1.6.6.rc1" and has no dataset routing); unparseable never routes.
  it.each([
    ["1.6.6", true],
    ["v1.6.6", true],
    ["1.6.6-local", true],
    ["1.6.6+local", true],
    ["1.6.6.post1", true],
    ["1.6.6.post1-local", true],
    ["1.6.6-local+abc", true],
    ["1.6.6-1", true],
    ["1.6.6.0", true],
    ["1.6.6.rc1", false],
    ["1.6.6rc1", false],
    ["1.6.6-rc1", false],
    ["1.6.6a1", false],
    ["1.6.6b2", false],
    ["1.6.6.alpha", false],
    ["1.6.6-beta.1", false],
    ["1.6.6.dev0", false],
    ["1.6.7rc1", true],
    ["1.7.0", true],
    ["1.7.0.dev0", true],
    ["1.7", true],
    ["2.0", true],
    ["1.6.5", false],
    ["1.6.5-local", false],
    ["1.6.5.dev0", false],
    ["0.5.3", false],
    ["unknown", false],
    ["1.6.6-custom", false],
    ["1.7.0-custom", true],
    ["1.6", false],
    ["2", false],
    ["", false],
  ])("routes from cognee 1.6.6: %s -> %s", (version, routes) => {
    expect(versionAtLeast(version, DATASET_ROUTING_MIN_VERSION)).toBe(routes);
  });
});

describe("CogneeHttpClient auto recall", () => {
  const originalFetch = global.fetch;
  afterEach(() => {
    global.fetch = originalFetch;
  });

  function server(version: string | Error) {
    const calls: Array<{ url: string; body?: Record<string, unknown> }> = [];
    global.fetch = jest.fn(async (url: string | URL, init?: RequestInit) => {
      const u = String(url);
      calls.push({ url: u, ...(init?.body ? { body: JSON.parse(String(init.body)) } : {}) });
      if (u.endsWith("/health")) {
        if (version instanceof Error) throw version;
        return new Response(JSON.stringify({ status: "ready", version }), { status: 200 });
      }
      return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
    }) as unknown as typeof fetch;
    return calls;
  }

  it("reads the version from /health once and caches the answer", async () => {
    const calls = server("1.6.6");
    const client = new CogneeHttpClient("http://cognee.test", "k");
    expect(await client.routesDatasets()).toBe(true);
    expect(await client.routesDatasets()).toBe(true);
    expect(calls.filter((c) => c.url.endsWith("/health"))).toHaveLength(1);
  });

  it("reads the version off an unhealthy server's 503", async () => {
    // cognee answers 503 while a critical store is down and still names its version.
    global.fetch = jest.fn(async () =>
      new Response(JSON.stringify({ status: "not ready", health: "unhealthy", version: "1.6.6" }), { status: 503 }),
    ) as unknown as typeof fetch;
    expect(await new CogneeHttpClient("http://cognee.test", "k").routesDatasets()).toBe(true);
    global.fetch = jest.fn(async () => new Response("<html>", { status: 502 })) as unknown as typeof fetch;
    expect(await new CogneeHttpClient("http://cognee.test", "k").routesDatasets()).toBe(false);
  });

  it("keeps recall scoped on an older server, a pre-release, or when /health fails", async () => {
    server("1.6.5");
    expect(await new CogneeHttpClient("http://cognee.test", "k").routesDatasets()).toBe(false);
    server("1.6.6.rc1");
    expect(await new CogneeHttpClient("http://cognee.test", "k").routesDatasets()).toBe(false);
    server("unknown");
    expect(await new CogneeHttpClient("http://cognee.test", "k").routesDatasets()).toBe(false);
    server(new Error("refused"));
    expect(await new CogneeHttpClient("http://cognee.test", "k").routesDatasets()).toBe(false);
  });

  describe("probe retry", () => {
    const HOUR = 60 * 60_000;
    const FIVE_MIN = 5 * 60_000;
    let now = 1_000_000_000;
    beforeEach(() => {
      now = 1_000_000_000;
      jest.spyOn(Date, "now").mockImplementation(() => now);
    });
    afterEach(() => jest.restoreAllMocks());
    const healthCalls = (calls: Array<{ url: string }>) => calls.filter((c) => c.url.endsWith("/health")).length;

    it("retries a failed probe with no history after five minutes, not on every prompt", async () => {
      let calls = server(new Error("refused"));
      const client = new CogneeHttpClient("http://cognee.test", "k");
      expect(await client.routesDatasets()).toBe(false);
      now += FIVE_MIN - 1;
      expect(await client.routesDatasets()).toBe(false);
      expect(healthCalls(calls)).toBe(1);
      calls = server("1.6.6");
      now += 2;
      expect(await client.routesDatasets()).toBe(true);
      expect(healthCalls(calls)).toBe(1);
    });

    it("keeps the last answer after a failed probe but re-probes within five minutes", async () => {
      // A downgrade during a restart: the failed probe must not keep routing on for an hour.
      server("1.6.6");
      const client = new CogneeHttpClient("http://cognee.test", "k");
      expect(await client.routesDatasets()).toBe(true);
      server(new Error("refused"));
      now += HOUR + 1;
      expect(await client.routesDatasets()).toBe(true);
      const calls = server("1.6.5");
      now += FIVE_MIN - 1;
      expect(await client.routesDatasets()).toBe(true);
      expect(healthCalls(calls)).toBe(0);
      now += 2;
      expect(await client.routesDatasets()).toBe(false);
      expect(healthCalls(calls)).toBe(1);
    });

    it("caches a successful probe for the hour", async () => {
      const calls = server("1.6.6");
      const client = new CogneeHttpClient("http://cognee.test", "k");
      expect(await client.routesDatasets()).toBe(true);
      now += HOUR - 1;
      expect(await client.routesDatasets()).toBe(true);
      expect(healthCalls(calls)).toBe(1);
    });
  });

  it("shares one in-flight probe between concurrent callers", async () => {
    let answer!: (r: Response) => void;
    const fetchMock = jest.fn(() => new Promise<Response>((resolve) => { answer = resolve; }));
    global.fetch = fetchMock as unknown as typeof fetch;
    const client = new CogneeHttpClient("http://cognee.test", "k");
    const both = Promise.all([client.routesDatasets(3_000), client.routesDatasets(3_000)]);
    answer(new Response(JSON.stringify({ status: "ready", version: "1.6.6" }), { status: 200 }));
    expect(await both).toEqual([true, true]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("bounds the probe by the timeout it is given, not the request timeout", async () => {
    global.fetch = jest.fn((_url: string | URL, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
    })) as unknown as typeof fetch;
    // Request timeout 120s; the probe gives up after 20ms and does not route.
    const client = new CogneeHttpClient("http://cognee.test", "k", undefined, undefined, 120_000);
    const started = Date.now();
    expect(await client.routesDatasets(20)).toBe(false);
    expect(Date.now() - started).toBeLessThan(2_000);
  });

  it("sends no dataset_ids when recall names no dataset, and the ids otherwise", async () => {
    const calls = server("1.6.6");
    const client = new CogneeHttpClient("http://cognee.test", "k");
    await client.recall({ queryText: "q", searchPrompt: "", datasetIds: [], scope: ["graph"] });
    await client.recall({ queryText: "q", searchPrompt: "", datasetIds: ["ds-1"], scope: ["graph"] });
    const bodies = calls.filter((c) => c.url.endsWith("/recall")).map((c) => c.body!);
    expect(bodies[0]).not.toHaveProperty("dataset_ids");
    expect(bodies[1].dataset_ids).toEqual(["ds-1"]);
  });
});

describe("memory_search auto recall", () => {
  const cfg = resolveConfig({ datasetName: "testds", minScore: 0, maxResults: 3 });

  async function search(d: MemoryToolsDeps) {
    const [tool] = createMemoryTools(d, { sessionId: "host-1" });
    return (await tool.execute("call-1", { query: "q" } as never)).details as MemorySearchResult;
  }

  it("sends one request naming no dataset when recall is auto", async () => {
    const calls: string[][] = [];
    const resolveDatasets = jest.fn(async () => [{ id: "ds-a", label: "agent" }]);
    const out = await search({
      cfg,
      resolveDatasets,
      autoRoute: async () => true,
      recall: async (p) => {
        calls.push(p.datasetIds);
        return [{ id: "x", text: "routed", score: 0.9 }];
      },
    });
    expect(calls).toEqual([[]]);
    expect(out.results).toHaveLength(1);
  });

  it("does not route while none of the agent's datasets resolves", async () => {
    // As in the prompt-time recall: routed, the server would only search other
    // agents' (or clients') datasets and label their hits as this agent's memory.
    const recall = jest.fn(async () => [{ id: "x", text: "someone else's", score: 0.9 }]);
    const autoRoute = jest.fn(async () => true);
    const out = await search({ cfg, resolveDatasets: async () => [], autoRoute, recall });
    expect(recall).not.toHaveBeenCalled();
    expect(autoRoute).not.toHaveBeenCalled();
    expect(out.results).toEqual([]);
    expect(out.note).toMatch(/No Cognee dataset is indexed yet/);
  });

  it("searches the conversation's datasets when recall is restricted", async () => {
    const calls: string[][] = [];
    await search({
      cfg,
      resolveDatasets: async () => [{ id: "ds-a", label: "agent" }],
      autoRoute: async () => false,
      recall: async (p) => {
        calls.push(p.datasetIds);
        return [];
      },
    });
    expect(calls).toEqual([["ds-a"]]);
  });

  const slow = <T>(ms: number, value: T) => new Promise<T>((resolve) => setTimeout(() => resolve(value), ms));

  it("takes only the routing probe's time out of the recall timeout", async () => {
    const timeouts: Array<number | undefined> = [];
    const out = await search({
      cfg: { ...cfg, recallTimeoutMs: 1_000 },
      // Slow dataset resolution (a listDatasets cache miss) is not the probe's time.
      resolveDatasets: () => slow(400, [{ id: "ds-a", label: "agent" }]),
      autoRoute: () => slow(300, true),
      recall: async (p) => {
        timeouts.push(p.timeoutMs);
        return [{ id: "x", text: "routed", score: 0.9 }];
      },
    });
    expect(out.results).toHaveLength(1);
    expect(timeouts).toHaveLength(1);
    expect(timeouts[0]).toBeLessThanOrEqual(710);
    // Had resolution been counted too, at most ~300 ms would be left.
    expect(timeouts[0]).toBeGreaterThan(450);
  });

  it("gives a pinned search the full recall timeout despite slow dataset resolution", async () => {
    // No explicit timeout, as before routing: the recall runs with the
    // configured recallTimeoutMs, the same request as an unrouted search.
    const params: Array<Record<string, unknown>> = [];
    const out = await search({
      cfg: { ...cfg, recallTimeoutMs: 100 },
      resolveDatasets: () => slow(250, [{ id: "ds-a", label: "agent" }]),
      autoRoute: async () => false,
      recall: async (p) => {
        params.push(p);
        return [{ id: "x", text: "pinned", score: 0.9 }];
      },
    });
    expect(out.results).toHaveLength(1);
    expect(params).toHaveLength(1);
    expect(params[0].datasetIds).toEqual(["ds-a"]);
    expect(params[0]).not.toHaveProperty("timeoutMs");
  });

  it("gives every multi-scope dataset the full recall timeout despite slow dataset resolution", async () => {
    // Multi-scope mode never routes (autoRoute answers false at once).
    const params: Array<Record<string, unknown>> = [];
    const out = await search({
      cfg: { ...cfg, recallTimeoutMs: 100 },
      resolveDatasets: () => slow(250, [{ id: "ds-a", label: "agent" }, { id: "ds-b", label: "user" }]),
      autoRoute: async () => false,
      recall: async (p) => {
        params.push(p);
        return [{ id: `x-${(p.datasetIds as string[])[0]}`, text: "hit", score: 0.9 }];
      },
    });
    expect(out.results).toHaveLength(2);
    expect(params.map((p) => p.datasetIds)).toEqual([["ds-a"], ["ds-b"]]);
    for (const p of params) expect(p).not.toHaveProperty("timeoutMs");
  });

  it("stops waiting for a probe that outlasts the recall timeout and searches the datasets with the full timeout", async () => {
    const params: Array<Record<string, unknown>> = [];
    const started = Date.now();
    const out = await search({
      cfg: { ...cfg, recallTimeoutMs: 50 },
      resolveDatasets: async () => [{ id: "ds-a", label: "agent" }],
      autoRoute: () => new Promise(() => {}),
      recall: async (p) => {
        params.push(p);
        return [{ id: "x", text: "pinned", score: 0.9 }];
      },
    });
    expect(Date.now() - started).toBeLessThan(1_000);
    // Not known to route: the conversation's datasets, sent like an unrouted
    // search (full recall timeout), not with what the probe left (nothing).
    expect(params.map((p) => p.datasetIds)).toEqual([["ds-a"]]);
    expect(params[0]).not.toHaveProperty("timeoutMs");
    expect(out.disabled).toBeUndefined();
    expect(out.results).toHaveLength(1);
    expect(out.results[0].text).toBe("pinned");
  });
});
