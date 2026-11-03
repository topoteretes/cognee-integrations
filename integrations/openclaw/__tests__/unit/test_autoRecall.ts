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
    expect(versionTuple("v1.7.0.dev0")).toEqual([1, 7, 0]);
    expect(versionTuple("")).toEqual([]);
  });

  it.each([
    ["1.6.6", true],
    ["1.6.6rc1", true],
    ["1.7.0", true],
    ["2.0", true],
    ["1.6.5", false],
    ["1.6.5.dev0", false],
    ["0.5.3", false],
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

  it("keeps recall scoped on an older server or when /health fails", async () => {
    server("1.6.5");
    expect(await new CogneeHttpClient("http://cognee.test", "k").routesDatasets()).toBe(false);
    server(new Error("refused"));
    expect(await new CogneeHttpClient("http://cognee.test", "k").routesDatasets()).toBe(false);
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
});
