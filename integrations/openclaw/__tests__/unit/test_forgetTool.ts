/**
 * memory_forget in isolation with fake server calls: the find phase's
 * listing/scan/ranking, the forget phase's confirmation gate and per-document
 * deletion, and the guarantees around scope (never wider than the listed
 * ids) and honesty (404 = already gone; unknown id = failed, not guessed).
 */

import { resolveConfig } from "../../src/config";
import {
  MAX_SCANNED_DOCUMENTS,
  createMemoryForgetTool,
  extractSessionId,
  preview,
  queryTerms,
  type MemoryForgetDeleteResult,
  type MemoryForgetDeps,
  type MemoryForgetFindResult,
} from "../../src/forget-tool";
import type { CogneeDataItem } from "../../src/types";

const cfg = resolveConfig({ datasetName: "testds" });

function item(id: string, name: string, createdAt: string, datasetId = "ds-1", meta?: Record<string, unknown>): CogneeDataItem {
  return { id, name, datasetId, createdAt, ...(meta ? { externalMetadata: meta } : {}) };
}

const RAW: Record<string, string> = {
  d1: "Session ID: sess-A\n\nQ: who won Wimbledon?\nA: Alcaraz. We talked about tennis rackets too.",
  d2: "Session ID: sess-A\n\nTrace feedback: the tennis lookup tool timed out once.",
  d3: "Session ID: sess-B\n\nQ: deploy plan?\nA: Fridays, after the standup.",
  d4: "Company handbook: vacation policy and expense rules.",
};

function deps(overrides: Partial<MemoryForgetDeps> = {}): MemoryForgetDeps & { forgetCalls: Array<{ datasetId: string; dataId: string }> } {
  const forgetCalls: Array<{ datasetId: string; dataId: string }> = [];
  return {
    cfg,
    resolveDatasets: async () => [{ id: "ds-1", label: "agent" }],
    listDatasetData: async () => [
      item("d1", "sess-A.txt", "2026-08-20T10:00:00Z"),
      item("d2", "sess-A-trace.txt", "2026-08-20T10:01:00Z"),
      item("d3", "sess-B.txt", "2026-08-21T10:00:00Z"),
      item("d4", "handbook.md.txt", "2026-08-01T10:00:00Z"),
    ],
    readRawData: async (_ds, id) => RAW[id] ?? "",
    forget: async (p) => { forgetCalls.push(p); return { deleted: true }; },
    forgetCalls,
    ...overrides,
  };
}

async function run(d: MemoryForgetDeps, params: Record<string, unknown>, ctx: Record<string, unknown> = { agentId: "will" }) {
  const tool = createMemoryForgetTool(d, ctx);
  return (await tool.execute("c", params as never)).details;
}

describe("helpers", () => {
  it("queryTerms lower-cases, drops short tokens and dedupes", () => {
    expect(queryTerms("the Tennis match, tennis!")).toEqual(["the", "tennis", "match"]);
    expect(queryTerms("a b")).toEqual([]);
  });

  it("extractSessionId prefers metadata, then the raw header", () => {
    expect(extractSessionId("Session ID: s-9\nbody", { session_id: "meta-1" })).toBe("meta-1");
    expect(extractSessionId("  Session ID: s-9\nbody")).toBe("s-9");
    expect(extractSessionId("no header")).toBeUndefined();
  });

  it("preview flattens whitespace and truncates", () => {
    expect(preview("a\n\n  b   c")).toBe("a b c");
    expect(preview("x".repeat(400))).toHaveLength(301);
  });
});

describe("memory_forget — find", () => {
  it("ranks candidates by matched terms, recovers session ids, and remembers their datasets", async () => {
    const d = deps();
    const out = (await run(d, { action: "find", query: "tennis rackets" })) as MemoryForgetFindResult;
    expect(out.action).toBe("find");
    expect(out.totalDocuments).toBe(4);
    expect(out.candidates.map((c) => [c.dataId, c.matchedTerms])).toEqual([
      ["d1", ["tennis", "rackets"]],
      ["d2", ["tennis"]],
    ]);
    expect(out.candidates[0]).toMatchObject({ datasetId: "ds-1", dataset: "agent", sessionId: "sess-A", name: "sess-A.txt" });
    expect(out.candidates[0].preview).toMatch(/^Session ID: sess-A Q: who won Wimbledon\?/);
    expect(out.note).toMatch(/Group documents from the same session/);
  });

  it("lists everything with previews when no query is given, capped by maxCandidates", async () => {
    const out = (await run(deps(), { action: "find", maxCandidates: 2 })) as MemoryForgetFindResult;
    expect(out.candidates).toHaveLength(2);
    // newest first
    expect(out.candidates.map((c) => c.dataId)).toEqual(["d3", "d2"]);
    expect(out.candidates.every((c) => c.matchedTerms.length === 0)).toBe(true);
  });

  it("tells the model not to delete on a miss", async () => {
    const out = (await run(deps(), { action: "find", query: "quantum chess" })) as MemoryForgetFindResult;
    expect(out.candidates).toEqual([]);
    expect(out.note).toMatch(/Do not delete 'closest' documents/);
  });

  it("bounds the raw scan and says so", async () => {
    const many = Array.from({ length: MAX_SCANNED_DOCUMENTS + 10 }, (_, i) => item(`x${i}`, `x${i}`, `2026-08-${String(1 + (i % 28)).padStart(2, "0")}T00:00:00Z`));
    const reads: string[] = [];
    const out = (await run(deps({ listDatasetData: async () => many, readRawData: async (_d, id) => { reads.push(id); return "tennis"; } }), { action: "find", query: "tennis" })) as MemoryForgetFindResult;
    expect(reads).toHaveLength(MAX_SCANNED_DOCUMENTS);
    expect(out.scannedDocuments).toBe(MAX_SCANNED_DOCUMENTS);
    expect(out.note).toMatch(/Only the 60 most recent of 70 documents were scanned/);
  });

  it("syncs the current session first when asked, and reports a failed sync", async () => {
    const sync = jest.fn(async () => {});
    const ok = (await run(deps({ syncSession: sync }), { action: "find", query: "tennis", syncSession: true }, { agentId: "will", sessionId: "host-1" })) as MemoryForgetFindResult;
    expect(sync).toHaveBeenCalledWith("host-1", "will");
    expect(ok.sessionSynced).toBe(true);

    const bad = (await run(deps({ syncSession: async () => { throw new Error("improve 500"); } }), { action: "find", query: "tennis", syncSession: true }, { agentId: "will", sessionId: "host-1" })) as MemoryForgetFindResult;
    expect(bad.sessionSynced).toBe(false);
    expect(bad.note).toMatch(/could not be synced/);

    const hint = (await run(deps(), { action: "find", query: "tennis" }, { agentId: "will", sessionId: "host-1" })) as MemoryForgetFindResult;
    expect(hint.note).toMatch(/pass syncSession=true/);
  });

  it("signals disabled when the server is unreachable, and empty when nothing is indexed", async () => {
    const down = (await run(deps({ listDatasetData: async () => { throw new Error("ECONNREFUSED"); } }), { action: "find" })) as MemoryForgetFindResult;
    expect(down.disabled).toBe(true);
    expect(down.error).toMatch(/ECONNREFUSED/);

    const none = (await run(deps({ resolveDatasets: async () => [] }), { action: "find" })) as MemoryForgetFindResult;
    expect(none.candidates).toEqual([]);
    expect(none.note).toMatch(/nothing to forget/);
  });
});

describe("memory_forget — forget", () => {
  it("refuses without dataIds and without confirm, deleting nothing", async () => {
    const d = deps();
    const noIds = (await run(d, { action: "forget", confirm: true })) as MemoryForgetDeleteResult;
    expect(noIds.error).toMatch(/dataIds is required/);
    const noConfirm = (await run(d, { action: "forget", dataIds: ["d1"] })) as MemoryForgetDeleteResult;
    expect(noConfirm.requiresConfirmation).toBe(true);
    expect(noConfirm.error).toMatch(/Nothing deleted.*1 document/);
    expect(d.forgetCalls).toEqual([]);
  });

  it("deletes exactly the confirmed ids, one call each, using the dataset from find", async () => {
    const d = deps();
    const tool = createMemoryForgetTool(d, { agentId: "will" });
    await tool.execute("c1", { action: "find", query: "tennis" });
    const out = (await tool.execute("c2", { action: "forget", dataIds: ["d1", "d2"], confirm: true })).details as MemoryForgetDeleteResult;

    expect(d.forgetCalls).toEqual([{ datasetId: "ds-1", dataId: "d1" }, { datasetId: "ds-1", dataId: "d2" }]);
    expect(out.deleted.map((x) => x.dataId)).toEqual(["d1", "d2"]);
    expect(out.failed).toEqual([]);
    expect(out.note).toMatch(/targeted, not whole-session/);
  });

  it("resolves ids it has not seen by listing, and fails cleanly on unknown ids", async () => {
    const d = deps();
    const out = (await run(d, { action: "forget", dataIds: ["d3", "ghost"], confirm: true })) as MemoryForgetDeleteResult;
    expect(d.forgetCalls).toEqual([{ datasetId: "ds-1", dataId: "d3" }]);
    expect(out.deleted.map((x) => x.dataId)).toEqual(["d3"]);
    expect(out.failed).toEqual([{ dataId: "ghost", error: expect.stringMatching(/unknown data id/) }]);
  });

  it("treats a 404 as already deleted and surfaces other errors as failures", async () => {
    const d = deps({
      forget: async ({ dataId }) => (dataId === "d1" ? { deleted: false, error: "HTTP (404) not found" } : { deleted: false, error: "HTTP (500) boom" }),
    });
    const out = (await run(d, { action: "forget", dataIds: ["d1", "d3"], confirm: true })) as MemoryForgetDeleteResult;
    expect(out.deleted.map((x) => x.dataId)).toEqual(["d1"]);
    expect(out.failed).toEqual([{ dataId: "d3", datasetId: "ds-1", error: "HTTP (500) boom" }]);
  });

  it("has no parameter that can express a dataset-wide or everything wipe", () => {
    const tool = createMemoryForgetTool(deps(), {});
    const props = Object.keys((tool.parameters as { properties: Record<string, unknown> }).properties);
    expect(props.sort()).toEqual(["action", "confirm", "dataIds", "maxCandidates", "query", "syncSession"]);
    expect((tool.parameters as { additionalProperties: boolean }).additionalProperties).toBe(false);
  });
});
