/**
 * Code-graph primitives: the identifier gate that keeps the code lane off
 * conversational prompts, per-repo dataset naming, code_query construction
 * for auto-recall and for the tool, the registry, the prompt section, and
 * the memory_code_search tool against a fake recall.
 */

import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { resolveConfig } from "../../src/config";
import {
  CodeGraphRegistry,
  buildCodeQuery,
  buildToolCodeQuery,
  canonicalSpec,
  createMemoryCodeSearchTool,
  defaultCodeDataset,
  extractIdentifiers,
  isRemoteRepo,
  renderCodeGraphSection,
  type CodeSearchDeps,
  type MemoryCodeSearchResult,
} from "../../src/code-graph";
import { loadCodeGraphs } from "../../src/persistence";

describe("extractIdentifiers", () => {
  it("stays off conversational prompts", () => {
    expect(extractIdentifiers("what did we decide about the rollout last week?")).toEqual([]);
    expect(extractIdentifiers("remind me to call Alice tomorrow")).toEqual([]);
    expect(extractIdentifiers("see example.com or v1.5.3, e.g. this")).toEqual([]);
  });

  it("fires on backticked symbols, file paths, dotted names, snake_case and CamelCase — in that order", () => {
    expect(extractIdentifiers("does `process_payment` call the DB?", 5)).toEqual(["process_payment"]);
    expect(extractIdentifiers("what breaks if src/plugin.ts changes and UserService moves?", 5)).toEqual(["src/plugin.ts", "UserService"]);
    expect(extractIdentifiers("look at auth.middleware.verify and handle_login", 5)).toEqual(["auth.middleware.verify", "handle_login"]);
  });

  it("drops stoplisted product names, dedupes case-insensitively, and honours the limit", () => {
    expect(extractIdentifiers("Cognee and OpenClaw and TypeScript are great")).toEqual([]);
    expect(extractIdentifiers("`FooBar` vs foobar vs FooBar", 5)).toEqual(["FooBar"]);
    expect(extractIdentifiers("a_b c_d e_f", 2)).toEqual(["a_b", "c_d"]);
  });
});

describe("repository specs", () => {
  it("recognizes remote specs and canonicalizes them", () => {
    expect(isRemoteRepo("https://github.com/x/y.git")).toBe(true);
    expect(isRemoteRepo("git@github.com:x/y.git")).toBe(true);
    expect(isRemoteRepo("./repo")).toBe(false);
    expect(canonicalSpec("https://github.com/x/y.git/")).toBe("https://github.com/x/y");
  });

  it("names datasets codebase-<tail>-<digest>, stable per spec and distinct per checkout", () => {
    const a = defaultCodeDataset("https://github.com/topoteretes/cognee");
    expect(a).toMatch(/^codebase-cognee-[0-9a-f]{8}$/);
    expect(defaultCodeDataset("https://github.com/topoteretes/cognee.git")).toBe(a);
    expect(defaultCodeDataset("https://github.com/other/cognee")).not.toBe(a);
  });
});

describe("code queries", () => {
  it("auto-recall uses a bounded query_facts lookup", () => {
    expect(buildCodeQuery("UserService")).toEqual({ operation: "query_facts", name: "UserService", limit: 5 });
  });

  it("tool queries seed each operation from `query` and validate required args", () => {
    expect(buildToolCodeQuery({ query: "UserService" })).toEqual({ operation: "query_facts", codeQuery: { operation: "query_facts", name: "UserService", limit: 10 } });
    expect(buildToolCodeQuery({ query: "main", operation: "traverse", args: { direction: "forward" } }).codeQuery).toEqual({ operation: "traverse", direction: "forward", start: "main" });
    expect(buildToolCodeQuery({ query: "pay", operation: "impact_analysis" }).codeQuery).toEqual({ operation: "impact_analysis", targets: ["pay"] });
    expect(buildToolCodeQuery({ operation: "find_path", args: { source: "A", target: "B" } }).error).toBeUndefined();
    expect(buildToolCodeQuery({ operation: "find_path" }).error).toMatch(/source and.*target/);
    expect(buildToolCodeQuery({ operation: "explore" }).error).toMatch(/needs a seed/);
    expect(buildToolCodeQuery({ operation: "delta" }).codeQuery).toEqual({ operation: "delta" });
    expect(buildToolCodeQuery({ operation: "bogus" as never, query: "x" }).operation).toBe("query_facts");
  });
});

describe("CodeGraphRegistry", () => {
  let dir: string;
  beforeEach(async () => { dir = await mkdtemp(join(tmpdir(), "cognee-cg-")); });
  afterEach(async () => rm(dir, { recursive: true, force: true }));

  it("upserts, lists newest first, persists and reloads", async () => {
    const path = join(dir, "code-graphs.json");
    let t = 0;
    const reg = new CodeGraphRegistry({ path, now: () => new Date(1_700_000_000_000 + (t++) * 1000) });
    await reg.ready();
    reg.upsert({ dataset: "codebase-a-1", spec: "/a", canonical: "/a", kind: "path", indexVectors: false });
    reg.upsert({ dataset: "codebase-b-2", spec: "https://x/b", canonical: "https://x/b", kind: "url", indexVectors: true });
    expect(reg.list().map((r) => r.dataset)).toEqual(["codebase-b-2", "codebase-a-1"]);
    await reg.flush();
    expect(Object.keys(await loadCodeGraphs(path)).sort()).toEqual(["codebase-a-1", "codebase-b-2"]);

    const again = new CodeGraphRegistry({ path });
    await again.ready();
    expect(again.get("codebase-b-2")?.indexVectors).toBe(true);
    expect(again.remove("codebase-a-1")).toBe(true);
    expect(again.remove("codebase-a-1")).toBe(false);
  });
});

describe("renderCodeGraphSection", () => {
  it("renders code-tagged facts and ignores everything else", () => {
    const out = renderCodeGraphSection(
      [
        { id: "1", text: "UserService.get -> Database.query", score: 1, source: "code" },
        { id: "2", text: "graph hit", score: 0.9, source: "graph" },
        { id: "3", text: "   ", score: 1, source: "code" },
      ],
      "UserService",
      "codebase-x-1",
    );
    expect(out).toHaveLength(1);
    expect(out[0]).toBe('<code_graph>\n[Deterministic code-graph facts for "UserService" from dataset codebase-x-1; exact, not semantic]\n- UserService.get -> Database.query\n</code_graph>');
    expect(renderCodeGraphSection([], "x", "d")).toEqual([]);
  });
});

describe("memory_code_search", () => {
  const cfg = resolveConfig({ datasetName: "testds" });
  function deps(overrides: Partial<CodeSearchDeps> = {}): CodeSearchDeps & { calls: unknown[] } {
    const calls: unknown[] = [];
    return {
      cfg,
      codeDatasets: async () => ["codebase-a-1"],
      resolveDatasetId: async (name) => (name === "codebase-a-1" ? "id-a" : undefined),
      recall: async (p) => { calls.push(p); return [{ id: "f1", text: "process_payment <- checkout()", score: 1, source: "code" }]; },
      calls,
      ...overrides,
    };
  }
  async function run(d: CodeSearchDeps, params: Record<string, unknown>) {
    return (await createMemoryCodeSearchTool(d, {}).execute("c", params as never)).details as MemoryCodeSearchResult;
  }

  it("queries the only indexed dataset with scope=code and the built code_query", async () => {
    const d = deps();
    const out = await run(d, { query: "process_payment" });
    expect(d.calls[0]).toMatchObject({ datasetIds: ["id-a"], scope: ["code"], codeQuery: { operation: "query_facts", name: "process_payment", limit: 10 } });
    expect(out).toMatchObject({ dataset: "codebase-a-1", operation: "query_facts", results: [{ text: "process_payment <- checkout()" }] });
  });

  it("explains when nothing is indexed, or when several are and none was named", async () => {
    const none = await run(deps({ codeDatasets: async () => [] }), { query: "x" });
    expect(none.error).toMatch(/No code graph is indexed.*index-repo/);
    const many = await run(deps({ codeDatasets: async () => ["a", "b"] }), { query: "x" });
    expect(many.error).toMatch(/pass `dataset`/);
    expect(many.availableDatasets).toEqual(["a", "b"]);
  });

  it("reports an unknown dataset, an unresolvable seed, and server failures without throwing", async () => {
    expect((await run(deps(), { query: "x", dataset: "ghost" })).error).toMatch(/not found on the server/);
    const empty = await run(deps({ recall: async () => [] }), { query: "Nope" });
    expect(empty.results).toEqual([]);
    expect(empty.note).toMatch(/unresolvable seed returns empty/);
    const down = await run(deps({ recall: async () => { throw new Error("fetch failed"); } }), { query: "x" });
    expect(down).toMatchObject({ disabled: true, error: "Error: fetch failed" });
    const ambiguous = await run(deps({ recall: async () => { throw new Error("HTTP (422) ambiguous seed: [A, B]"); } }), { query: "x" });
    expect(ambiguous.disabled).toBeUndefined();
    expect(ambiguous.error).toMatch(/ambiguous/);
  });

  it("validates operation arguments before touching the server", async () => {
    const d = deps();
    const out = await run(d, { operation: "find_path" });
    expect(out.error).toMatch(/source and.*target/);
    expect(d.calls).toEqual([]);
  });
});
