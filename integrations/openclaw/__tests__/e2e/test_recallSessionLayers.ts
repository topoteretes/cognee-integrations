/**
 * Session layers on the prompt path: with sessions enabled, recall issues the
 * graph lane(s) AND one explicit session-layers call (scope session/trace/
 * session_context, context_profile=agent), and injects each non-empty layer
 * as its own block ahead of the graph results. The lane is skipped without a
 * session, is off-switchable, never blocks a turn when it fails, and the
 * memory_search tool's corpus=sessions uses the same explicit scope.
 */

import plugin from "../../src/plugin";
import { CogneeHttpClient } from "../../src/client";
import { createPluginApi } from "../../test-utils/fakeApi";

jest.mock("../../src/client");
jest.mock("../../src/server", () => {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const { serverMock: mk } = require("../../test-utils/fakeApi");
  return mk();
});

const mockBreaker = {
  openForSeconds: jest.fn(async () => 0),
  recordFailure: jest.fn(async (_msg: string) => {}),
  recordSuccess: jest.fn(async () => {}),
};
jest.mock("../../src/breaker", () => ({
  RecallBreaker: jest.fn(() => mockBreaker),
  isBreakerError: () => false,
}));

let datasetState: Record<string, string> = {};
jest.mock("../../src/persistence", () => ({
  loadDatasetState: jest.fn(async () => ({ ...datasetState })),
  saveDatasetState: jest.fn(async (s: Record<string, string>) => { datasetState = { ...s }; }),
  loadSyncIndex: jest.fn(async () => ({ entries: {} })),
  saveSyncIndex: jest.fn(async () => {}),
  loadScopedSyncIndexes: jest.fn(async () => ({})),
  saveScopedSyncIndexes: jest.fn(async () => {}),
  loadAgentSyncIndexes: jest.fn(async () => ({})),
  saveAgentSyncIndexes: jest.fn(async () => {}),
  migrateLegacyIndex: jest.fn(async () => null),
  migrateAgentScopeToPerAgent: jest.fn(async () => null),
  SYNC_INDEX_PATH: "/tmp/sync-index.json",
}));

type RecallParams = { datasetIds: string[]; scope?: string[]; sessionId?: string; contextProfile?: string };
const mockRecall = jest.fn(async (_p: RecallParams): Promise<unknown[]> => []);

const GRAPH_HIT = { id: "g1", text: "User prefers dark mode", score: 0.9, source: "graph" };
const SESSION_HITS = [
  { id: "c1", text: "Always confirm before deleting.", score: 1, source: "session_context" },
  { id: "t1", text: "deploy (error)\nLesson: retry with --wait", score: 1, source: "trace" },
  { id: "q1", text: "Q: theme?\nA: dark", score: 1, source: "session" },
];

function isSessionLane(p: RecallParams): boolean {
  return Array.isArray(p.scope) && p.scope.includes("session");
}

async function runPrompt(pluginConfig: Record<string, unknown> = {}, ctx: Record<string, unknown> = { agentId: "will", sessionId: "s1" }) {
  const harness = createPluginApi(plugin, { autoRecall: true, enableSessions: true, captureSession: false, minScore: 0, ...pluginConfig });
  let injection: unknown;
  // fakeApi.emit discards return values; capture the recall handler's result directly.
  const handlers = (harness.api.on as jest.Mock).mock.calls.filter((c) => c[0] === "before_prompt_build").map((c) => c[1]);
  for (const h of handlers) {
    const r = await h({ prompt: "what did we decide about the theme?" }, ctx);
    if (r !== undefined) injection = r;
  }
  return { harness, injection: injection as Record<string, string> | undefined };
}

beforeEach(() => {
  jest.clearAllMocks();
  datasetState = { testds: "ds-1" };
  mockBreaker.openForSeconds.mockImplementation(async () => 0);
  mockRecall.mockImplementation(async (p) => (isSessionLane(p) ? SESSION_HITS : [GRAPH_HIT]));
  (CogneeHttpClient as unknown as jest.Mock).mockImplementation(() => ({
    recall: mockRecall,
    rememberEntry: jest.fn(async () => ({ entryId: "e1" })),
    registerAgent: jest.fn(async () => ({ ok: true, connectionId: "c1" })),
    unregisterAgent: jest.fn(async () => ({ ok: true, activeAgents: 0 })),
    improve: jest.fn(async () => ({ status: "ok" })),
    health: jest.fn(async () => ({ status: "ok" })),
    listDatasets: jest.fn(async () => [{ id: "ds-1", name: "testds" }]),
    setApiKey: jest.fn(),
  }));
});

describe("session-layers lane (single-scope)", () => {
  it("issues an explicit session-scoped call alongside the graph lane and injects layered blocks first", async () => {
    const { injection } = await runPrompt();

    expect(mockRecall).toHaveBeenCalledTimes(2);
    const lane = mockRecall.mock.calls.map((c) => c[0]).find(isSessionLane)!;
    expect(lane).toMatchObject({
      scope: ["session", "trace", "session_context"],
      contextProfile: "agent",
      sessionId: "open_claw_s1",
      datasetIds: ["ds-1"],
    });
    const graph = mockRecall.mock.calls.map((c) => c[0]).find((p) => !isSessionLane(p))!;
    expect(graph.scope).toBeUndefined();

    const text = injection?.prependContext ?? "";
    const order = ["<agent_guidance>", "<trace_lessons>", "<session_memory>", "<graph_memory>"].map((t) => text.indexOf(t));
    expect(order.every((i) => i >= 0)).toBe(true);
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(text).toContain("Always confirm before deleting.");
    expect(text).toContain("User prefers dark mode");
  });

  it("injects session layers even when the graph lane is empty", async () => {
    mockRecall.mockImplementation(async (p) => (isSessionLane(p) ? SESSION_HITS.slice(0, 1) : []));
    const { injection } = await runPrompt();
    expect(injection?.prependContext).toContain("<agent_guidance>");
    expect(injection?.prependContext).not.toContain("<graph_memory>");
  });

  it("skips the lane without a session, and when recallSessionLayers is false", async () => {
    await runPrompt({}, { agentId: "will" });
    expect(mockRecall.mock.calls.some((c) => isSessionLane(c[0]))).toBe(false);

    jest.clearAllMocks();
    await runPrompt({ recallSessionLayers: false });
    expect(mockRecall).toHaveBeenCalledTimes(1);
    expect(isSessionLane(mockRecall.mock.calls[0][0])).toBe(false);
  });

  it("a failing session lane never costs the turn its graph results", async () => {
    mockRecall.mockImplementation(async (p) => { if (isSessionLane(p)) throw new Error("cache down"); return [GRAPH_HIT]; });
    const { harness, injection } = await runPrompt();
    expect(injection?.prependContext).toContain("User prefers dark mode");
    expect(injection?.prependContext).not.toContain("<session_memory>");
    expect(harness.logger.warn).toHaveBeenCalledWith(expect.stringContaining("session-layer recall failed"));
  });
});

describe("session-layers lane (multi-scope)", () => {
  it("runs once across all scope datasets and sits ahead of the per-scope graph blocks", async () => {
    datasetState = { acme: "ds-company", "acme-agent-will": "ds-agent" };
    const { injection } = await runPrompt({ companyDataset: "acme", agentDatasetPrefix: "acme-agent", recallScopes: ["agent", "company"] });

    const lanes = mockRecall.mock.calls.map((c) => c[0]).filter(isSessionLane);
    expect(lanes).toHaveLength(1);
    expect(lanes[0].datasetIds.sort()).toEqual(["ds-agent", "ds-company"]);
    expect(mockRecall).toHaveBeenCalledTimes(3);

    const text = injection?.prependContext ?? "";
    expect(text.indexOf("<agent_guidance>")).toBeLessThan(text.indexOf("<agent_memory>"));
    expect(text).toContain("<company_memory>");
  });
});

describe("memory_search corpus=sessions", () => {
  it("requests the session layers explicitly and labels provenance by layer", async () => {
    const harness = createPluginApi(plugin, { autoRecall: false, enableSessions: true });
    const [searchTool] = harness.tools({ agentId: "will", sessionId: "s1" });
    const res = (await searchTool.execute("c", { query: "theme?", corpus: "sessions" })) as { details: { results: Array<{ source: string; scope: string }> } };

    expect(mockRecall).toHaveBeenCalledTimes(1);
    expect(mockRecall.mock.calls[0][0]).toMatchObject({ scope: ["session", "trace", "session_context"], contextProfile: "agent", sessionId: "open_claw_s1" });
    expect(res.details.results.map((r) => r.source).sort()).toEqual(["agent guidance", "session", "trace"]);
    expect(res.details.results.every((r) => r.scope === "session")).toBe(true);
  });
});
