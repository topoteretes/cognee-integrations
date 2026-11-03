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

  // A release and its local/post labels count as the release; a pre-release
  // or dev build sorts below it (cognee 1.6.6rc1 reports "1.6.6.rc1" and has
  // no dataset routing); unparseable never routes.
  it.each([
    ["1.6.6", true],
    ["v1.6.6", true],
    ["1.6.6-local", true],
    ["1.6.6+local", true],
    ["1.6.6.post1", true],
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
    ["2.0", true],
    ["1.6.5", false],
    ["1.6.5-local", false],
    ["1.6.5.dev0", false],
    ["0.5.3", false],
    ["unknown", false],
    ["1.6.6-custom", false],
    ["1.7.0-custom", false],
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
    expect(resolveDatasets).not.toHaveBeenCalled();
    expect(out.results).toHaveLength(1);
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

  it("takes the routing probe's time out of the recall timeout", async () => {
    const timeouts: Array<number | undefined> = [];
    await search({
      cfg: { ...cfg, recallTimeoutMs: 1_000 },
      resolveDatasets: async () => [{ id: "ds-a", label: "agent" }],
      autoRoute: () => new Promise((resolve) => setTimeout(() => resolve(true), 300)),
      recall: async (p) => {
        timeouts.push(p.timeoutMs);
        return [];
      },
    });
    expect(timeouts).toHaveLength(1);
    expect(timeouts[0]).toBeLessThanOrEqual(710);
    expect(timeouts[0]).toBeGreaterThan(0);
  });

  it("stops waiting for a probe that outlasts the recall timeout", async () => {
    const calls: string[][] = [];
    const started = Date.now();
    await search({
      cfg: { ...cfg, recallTimeoutMs: 50 },
      resolveDatasets: async () => [{ id: "ds-a", label: "agent" }],
      autoRoute: () => new Promise(() => {}),
      recall: async (p) => {
        calls.push(p.datasetIds);
        return [];
      },
    });
    expect(Date.now() - started).toBeLessThan(1_000);
    // Not known to route: the conversation's datasets.
    expect(calls).toEqual([["ds-a"]]);
  });
});
