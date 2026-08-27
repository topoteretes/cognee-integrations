/**
 * memory_search / memory_get in isolation: reference handles, provenance
 * extraction, corpus routing, unavailability signalling, dedupe/rank/cap,
 * and workspace-file excerpts. The recall function is a fake so every branch
 * is reachable without a server.
 */

import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { resolveConfig } from "../../src/config";
import {
  ReferenceCache,
  createMemoryTools,
  hitSource,
  hitTime,
  isMemoryFilePath,
  makeReference,
  parseReference,
  readMemoryFileExcerpt,
  toHit,
  type MemoryGetResult,
  type MemorySearchResult,
  type MemoryToolsDeps,
} from "../../src/tools";
import type { CogneeSearchResult } from "../../src/types";

const cfg = resolveConfig({ datasetName: "testds", minScore: 0, maxResults: 3 });

function r(id: string, text: string, score: number, metadata?: Record<string, unknown>): CogneeSearchResult {
  return { id, text, score, ...(metadata ? { metadata } : {}) };
}

function deps(overrides: Partial<MemoryToolsDeps> = {}): MemoryToolsDeps {
  return {
    cfg,
    resolveDatasets: async () => [{ id: "ds-1", label: "agent" }],
    recall: async () => [],
    ...overrides,
  };
}

async function search(d: MemoryToolsDeps, params: Record<string, unknown>, ctx = {}) {
  const [tool] = createMemoryTools(d, ctx);
  const res = await tool.execute("call-1", params as never);
  return res.details as MemorySearchResult;
}

async function get(d: MemoryToolsDeps, params: Record<string, unknown>, ctx = {}) {
  const [, tool] = createMemoryTools(d, ctx);
  const res = await tool.execute("call-2", params as never);
  return res.details as MemoryGetResult;
}

describe("references", () => {
  it("round-trips scope and id, including ids with slashes", () => {
    const ref = makeReference("graph", "a/b c");
    expect(ref).toBe("cognee://graph/a%2Fb%20c");
    expect(parseReference(ref)).toEqual({ scope: "graph", id: "a/b c" });
  });

  it("rejects malformed handles", () => {
    expect(parseReference("MEMORY.md")).toBeNull();
    expect(parseReference("cognee://wiki/x")).toBeNull();
    expect(parseReference("cognee://graph/")).toBeNull();
  });

  it("cache is bounded and evicts oldest", () => {
    const c = new ReferenceCache(2);
    for (const id of ["1", "2", "3"]) c.put(toHit(r(id, "t", 1), "graph", "agent"));
    expect(c.size).toBe(2);
    expect(c.get(makeReference("graph", "1"))).toBeUndefined();
    expect(c.get(makeReference("graph", "3"))?.text).toBe("t");
  });
});

describe("provenance", () => {
  it("derives source from metadata, stripping paths and .txt", () => {
    expect(hitSource(r("1", "", 1, { source: "memory/company/handbook.md.txt" }), "agent")).toBe("handbook.md");
    expect(hitSource(r("1", "", 1), "agent")).toBe("agent");
  });

  it("normalizes time from string or epoch metadata", () => {
    expect(hitTime(r("1", "", 1, { created_at: "2026-08-01T00:00:00Z" }))).toBe("2026-08-01T00:00:00Z");
    expect(hitTime(r("1", "", 1, { timestamp: 1_700_000_000 }))).toBe("2023-11-14T22:13:20.000Z");
    expect(hitTime(r("1", "", 1))).toBeUndefined();
  });

  it("toHit fills memory-core aliases and truncates snippet", () => {
    const hit = toHit(r("x", "a".repeat(500), 0.8), "session", "session");
    expect(hit.path).toBe(hit.reference);
    expect(hit.snippet).toHaveLength(401);
    expect(hit.scope).toBe("session");
  });
});

describe("memory_search", () => {
  it("returns ranked, deduped, capped results and caches references", async () => {
    const cache = new ReferenceCache();
    const d = deps({
      cache,
      resolveDatasets: async () => [{ id: "ds-a", label: "agent" }, { id: "ds-u", label: "user" }],
      recall: async ({ datasetIds }) =>
        datasetIds[0] === "ds-a"
          ? [r("1", "low", 0.2), r("2", "high", 0.9), r("2", "high dup", 0.9)]
          : [r("3", "mid", 0.5), r("4", "mid2", 0.4)],
    });
    const out = await search(d, { query: "what do we know?", corpus: "memory" });
    expect(out.results.map((h) => [h.text, h.source])).toEqual([["high", "agent"], ["mid", "user"], ["mid2", "user"]]);
    expect(out.disabled).toBeUndefined();
    expect(cache.size).toBe(3);
  });

  it("honours maxResults and minScore overrides", async () => {
    const d = deps({ recall: async () => [r("1", "a", 0.9), r("2", "b", 0.6), r("3", "c", 0.3)] });
    const out = await search(d, { query: "q", maxResults: 1, minScore: 0.5 });
    expect(out.results.map((h) => h.text)).toEqual(["a"]);
  });

  it("corpus=all adds a session-cache pass when a session id resolves; corpus=memory does not", async () => {
    const calls: Array<string | undefined> = [];
    const d = deps({
      sessionIdFor: (s) => (s ? `cog_${s}` : undefined),
      recall: async ({ sessionId }) => { calls.push(sessionId); return [r(sessionId ? "s1" : "g1", sessionId ? "from session" : "from graph", 0.7)]; },
    });
    const all = await search(d, { query: "q" }, { sessionId: "host-1" });
    expect(calls).toEqual([undefined, "cog_host-1"]);
    expect(all.results.map((h) => h.scope).sort()).toEqual(["graph", "session"]);

    calls.length = 0;
    await search(d, { query: "q", corpus: "memory" }, { sessionId: "host-1" });
    expect(calls).toEqual([undefined]);
  });

  it("corpus=sessions without a session yields no results and no recall", async () => {
    const recall = jest.fn(async () => []);
    const out = await search(deps({ recall }), { query: "q", corpus: "sessions" });
    expect(out.results).toEqual([]);
    expect(recall).not.toHaveBeenCalled();
  });

  it("signals disabled when every recall fails, warns when only some do", async () => {
    const failing = deps({ recall: async () => { throw new Error("ECONNREFUSED"); } });
    const out = await search(failing, { query: "q" });
    expect(out).toMatchObject({ results: [], disabled: true, unavailable: true, error: "Error: ECONNREFUSED" });
    expect(out.action).toMatch(/retry memory_search/);

    const partial = deps({
      resolveDatasets: async () => [{ id: "ok", label: "agent" }, { id: "bad", label: "user" }],
      recall: async ({ datasetIds }) => { if (datasetIds[0] === "bad") throw new Error("boom"); return [r("1", "t", 0.9)]; },
    });
    const half = await search(partial, { query: "q", corpus: "memory" });
    expect(half.results).toHaveLength(1);
    expect(half.disabled).toBeUndefined();
    expect(half.warning).toMatch(/Some scopes failed: Error: boom/);
  });

  it("signals disabled while the breaker is open, without calling recall", async () => {
    const recall = jest.fn(async () => []);
    const out = await search(deps({ recall, breakerOpenForSeconds: async () => 42 }), { query: "q" });
    expect(out.disabled).toBe(true);
    expect(out.error).toMatch(/breaker open \(retry in 42s\)/);
    expect(recall).not.toHaveBeenCalled();
  });

  it("explains an empty dataset set and an unsupported wiki corpus without erroring", async () => {
    expect((await search(deps({ resolveDatasets: async () => [] }), { query: "q" })).note).toMatch(/No Cognee dataset/);
    const wiki = await search(deps(), { query: "q", corpus: "wiki" });
    expect(wiki.results).toEqual([]);
    expect(wiki.note).toMatch(/wiki/);
    expect((await search(deps(), { query: "   " })).error).toBe("query is required");
  });

  it("renders details as the text content too", async () => {
    const [tool] = createMemoryTools(deps({ recall: async () => [r("1", "hello", 1)] }), {});
    const res = await tool.execute("c", { query: "q" });
    expect(res.content[0].type).toBe("text");
    expect(JSON.parse(res.content[0].text).results[0].text).toBe("hello");
  });
});

describe("memory_get", () => {
  it("resolves a cached reference with provenance, and reports stale ones structurally", async () => {
    const cache = new ReferenceCache();
    const d = deps({ cache, recall: async () => [r("1", "full text here", 0.8, { source: "MEMORY.md", created_at: "2026-01-01T00:00:00Z" })] });
    const found = await search(d, { query: "q" });
    const ref = found.results[0].reference;

    const got = await get(d, { path: ref });
    expect(got).toEqual({ path: ref, text: "full text here", source: "MEMORY.md", scope: "graph", score: 0.8, time: "2026-01-01T00:00:00Z" });

    const stale = await get(d, { path: makeReference("graph", "nope") });
    expect(stale.text).toBe("");
    expect(stale.error).toMatch(/reference not found/);
  });

  it("rejects paths that are neither references nor memory files", async () => {
    const out = await get(deps(), { path: "src/plugin.ts" }, { workspaceDir: "/tmp" });
    expect(out.error).toMatch(/cognee:\/\/ reference .* or a workspace memory file/);
    expect((await get(deps(), { path: "MEMORY.md" })).error).toMatch(/no workspace directory/);
    expect((await get(deps(), { path: "x", corpus: "wiki" })).disabled).toBe(true);
  });
});

describe("workspace memory file excerpts", () => {
  let ws: string;
  beforeAll(async () => {
    ws = await mkdtemp(join(tmpdir(), "cognee-tools-ws-"));
    await mkdir(join(ws, "memory"), { recursive: true });
    await writeFile(join(ws, "MEMORY.md"), Array.from({ length: 10 }, (_, i) => `line ${i + 1}`).join("\n"));
    await writeFile(join(ws, "memory", "notes.md"), "only line");
  });
  afterAll(async () => { await rm(ws, { recursive: true, force: true }); });

  it("accepts MEMORY.md and memory/** only", () => {
    expect(isMemoryFilePath("MEMORY.md")).toBe(true);
    expect(isMemoryFilePath("./memory/notes.md")).toBe(true);
    expect(isMemoryFilePath("../MEMORY.md")).toBe(false);
    expect(isMemoryFilePath("/etc/passwd")).toBe(false);
    expect(isMemoryFilePath("README.md")).toBe(false);
  });

  it("returns a bounded excerpt with continuation info", async () => {
    const out = await readMemoryFileExcerpt(ws, "MEMORY.md", 3, 4);
    expect(out).toMatchObject({ path: "MEMORY.md", scope: "file", from: 3, lines: 4, totalLines: 10, truncated: true, nextFrom: 7 });
    expect(out.text).toBe("line 3\nline 4\nline 5\nline 6");
    const tail = await readMemoryFileExcerpt(ws, "MEMORY.md", 9);
    expect(tail).toMatchObject({ lines: 2, truncated: false });
    expect(tail.nextFrom).toBeUndefined();
  });

  it("reports missing files and blocks traversal", async () => {
    expect((await readMemoryFileExcerpt(ws, "memory/missing.md")).error).toBe("file not found");
    expect((await readMemoryFileExcerpt(ws, "memory/../../etc/passwd")).error).toMatch(/escapes the workspace/);
  });

  it("is reachable through the tool with the workspace from the tool context", async () => {
    const out = await get(deps(), { path: "memory/notes.md" }, { workspaceDir: ws });
    expect(out.text).toBe("only line");
  });
});
