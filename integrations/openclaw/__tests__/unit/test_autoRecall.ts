/**
 * Auto recall (SDK-1393): graph recall that names no dataset, routed by a
 * cognee >= 1.6.6 server to the best-matching dataset(s). Covers the request
 * shape and memory_search's single routed request. The per-conversation
 * recall mode lives in test_datasetSwitch.ts.
 */

import { CogneeHttpClient } from "../../src/client";
import { resolveConfig } from "../../src/config";
import { createMemoryTools, type MemorySearchResult, type MemoryToolsDeps } from "../../src/tools";

describe("CogneeHttpClient auto recall", () => {
  const originalFetch = global.fetch;
  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("sends no dataset_ids when recall names no dataset, and the ids otherwise", async () => {
    const calls: Array<{ url: string; body?: Record<string, unknown> }> = [];
    global.fetch = jest.fn(async (url: string | URL, init?: RequestInit) => {
      calls.push({ url: String(url), ...(init?.body ? { body: JSON.parse(String(init.body)) } : {}) });
      return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
    }) as unknown as typeof fetch;
    const client = new CogneeHttpClient("http://cognee.test", "k");
    await client.recall({ queryText: "q", searchPrompt: "", datasetIds: [], scope: ["graph"] });
    await client.recall({ queryText: "q", searchPrompt: "", datasetIds: ["ds-1"], scope: ["graph"] });
    const bodies = calls.filter((c) => c.url.endsWith("/recall")).map((c) => c.body!);
    expect(bodies[0]).not.toHaveProperty("dataset_ids");
    expect(bodies[1].dataset_ids).toEqual(["ds-1"]);
    // No version probe: routing is not gated on the server version.
    expect(calls.filter((c) => c.url.endsWith("/health"))).toHaveLength(0);
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

  it("searches every multi-scope dataset, one request each, when recall does not route", async () => {
    const calls: string[][] = [];
    const out = await search({
      cfg,
      resolveDatasets: async () => [{ id: "ds-a", label: "agent" }, { id: "ds-b", label: "user" }],
      autoRoute: async () => false,
      recall: async (p) => {
        calls.push(p.datasetIds);
        return [{ id: `x-${p.datasetIds[0]}`, text: "hit", score: 0.9 }];
      },
    });
    expect(out.results).toHaveLength(2);
    expect(calls).toEqual([["ds-a"], ["ds-b"]]);
  });
});
