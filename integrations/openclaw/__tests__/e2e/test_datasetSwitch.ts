/**
 * Dataset switching through the plugin: after memory_switch_dataset, the SAME
 * conversation captures into the new dataset under a suffixed Cognee session
 * id, recalls from the new dataset, and improves the new dataset at session
 * end — while an unrelated conversation of the same agent is untouched.
 */

import plugin from "../../src/plugin";
import { CogneeHttpClient } from "../../src/client";
import { DatasetSwitchStore } from "../../src/dataset-switch";
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
let overridesOnDisk: Record<string, unknown> = {};
jest.mock("../../src/persistence", () => ({
  loadDatasetState: jest.fn(async () => ({ ...datasetState })),
  saveDatasetState: jest.fn(async (s: Record<string, string>) => { datasetState = { ...s }; }),
  DATASET_OVERRIDES_PATH: "/tmp/dataset-overrides.json",
  loadDatasetOverrides: jest.fn(async () => ({ ...overridesOnDisk })),
  saveDatasetOverrides: jest.fn(async (d: Record<string, unknown>) => { overridesOnDisk = { ...d }; }),
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

type RecallParams = { datasetIds: string[]; scope?: string[]; sessionId?: string };
const mockRecall = jest.fn(async (_p: RecallParams): Promise<unknown[]> => []);
const mockRememberEntry = jest.fn(async (_p: unknown) => ({ entryId: "e1" }));
const mockImprove = jest.fn(async (_p: unknown) => ({ status: "PipelineRunCompleted" }));
const mockEnsureDataset = jest.fn(async (name: string) => `id-${name}`);

async function flush(rounds = 20): Promise<void> {
  for (let i = 0; i < rounds; i++) await new Promise((r) => setTimeout(r, 0));
}

const CONVO_A = { agentId: "will", sessionId: "s1", sessionKey: "agent:will:tg-1" };
const CONVO_B = { agentId: "will", sessionId: "s2", sessionKey: "agent:will:tg-2" };

function harness(extra: Record<string, unknown> = {}) {
  return createPluginApi(plugin, { autoRecall: true, enableSessions: true, captureSession: true, minScore: 0, ...extra });
}

beforeEach(async () => {
  jest.clearAllMocks();
  DatasetSwitchStore.resetShared();
  overridesOnDisk = {};
  datasetState = { testds: "ds-1" };
  mockBreaker.openForSeconds.mockImplementation(async () => 0);
  mockRecall.mockImplementation(async () => []);
  (CogneeHttpClient as unknown as jest.Mock).mockImplementation(() => ({
    recall: mockRecall,
    rememberEntry: mockRememberEntry,
    improve: mockImprove,
    ensureDataset: mockEnsureDataset,
    registerAgent: jest.fn(async () => ({ ok: true, connectionId: "c1" })),
    unregisterAgent: jest.fn(async () => ({ ok: true, activeAgents: 0 })),
    health: jest.fn(async () => ({ status: "ok" })),
    listDatasets: jest.fn(async () => [{ id: "ds-1", name: "testds" }, { id: "id-proj-a", name: "proj-a" }]),
    setApiKey: jest.fn(),
  }));
});

async function switchTo(h: ReturnType<typeof harness>, dataset: string, ctx = CONVO_A) {
  const tool = h.tools(ctx).find((t) => t.name === "memory_switch_dataset")!;
  return (await tool.execute("c", { action: "switch", dataset })) as { details: Record<string, unknown> };
}

describe("memory_switch_dataset end to end", () => {
  it("syncs the old session, then routes capture to the new dataset under a suffixed session id", async () => {
    const h = harness();
    const res = await switchTo(h, "proj-a");
    expect(res.details).toMatchObject({ switched: true, dataset: "proj-a", sessionId: "open_claw_s1__2", previous: { dataset: "testds", sessionId: "open_claw_s1", synced: true } });
    expect(mockImprove).toHaveBeenCalledWith({ datasetName: "testds", sessionIds: ["open_claw_s1"], runInBackground: false });
    expect(mockEnsureDataset).toHaveBeenCalledWith("proj-a");
    expect(datasetState["proj-a"]).toBe("id-proj-a");

    await h.emit("before_prompt_build", { prompt: "what is the plan for proj-a?" }, CONVO_A);
    await h.emit("llm_output", { assistantTexts: ["ship on friday"] }, CONVO_A);
    await flush();

    const qa = mockRememberEntry.mock.calls.map((c) => c[0] as { datasetName: string; sessionId: string; entry: { type: string } }).find((c) => c.entry.type === "qa")!;
    expect(qa.datasetName).toBe("proj-a");
    expect(qa.sessionId).toBe("open_claw_s1__2");
  });

  it("recalls from the new dataset (graph lane) and the new session (session lane)", async () => {
    const h = harness();
    await switchTo(h, "proj-a");
    mockRecall.mockClear();

    await h.emit("before_prompt_build", { prompt: "what is the plan for proj-a?" }, CONVO_A);
    const calls = mockRecall.mock.calls.map((c) => c[0]);
    const graph = calls.find((c) => !c.scope)!;
    const session = calls.find((c) => Array.isArray(c.scope))!;
    expect(graph.datasetIds).toEqual(["id-proj-a"]);
    expect(session.sessionId).toBe("open_claw_s1__2");
    expect(session.datasetIds).toEqual(["id-proj-a"]);
  });

  it("leaves another conversation of the same agent on the configured dataset", async () => {
    const h = harness();
    await switchTo(h, "proj-a");
    mockRecall.mockClear();
    mockRememberEntry.mockClear();

    await h.emit("before_prompt_build", { prompt: "unrelated chat question" }, CONVO_B);
    await h.emit("llm_output", { assistantTexts: ["answer"] }, CONVO_B);
    await flush();

    expect(mockRecall.mock.calls.map((c) => c[0]).find((c) => !c.scope)!.datasetIds).toEqual(["ds-1"]);
    const qa = mockRememberEntry.mock.calls.map((c) => c[0] as { datasetName: string; sessionId: string }).pop()!;
    expect(qa).toMatchObject({ datasetName: "testds", sessionId: "open_claw_s2" });
  });

  it("improves the new dataset with the suffixed session id at session end", async () => {
    const h = harness();
    // gateway_start resolves serviceReady, which the final chain awaits.
    await h.emit("gateway_start", { port: 1 }, {});
    await flush();
    await switchTo(h, "proj-a");
    await h.emit("before_prompt_build", { prompt: "hello there" }, CONVO_A);
    await flush();
    mockImprove.mockClear();

    await h.emit("session_end", { sessionId: "s1", sessionKey: CONVO_A.sessionKey, messageCount: 2 }, CONVO_A);
    await flush(40);

    const call = mockImprove.mock.calls.map((c) => c[0] as { datasetName: string; sessionIds: string[] }).pop()!;
    expect(call).toMatchObject({ datasetName: "proj-a", sessionIds: ["open_claw_s1__2"] });
  });

  it("a forced switch after a failed sync re-syncs the retired session into its old dataset at session end", async () => {
    const h = harness();
    await h.emit("gateway_start", { port: 1 }, {});
    await flush();
    await h.emit("before_prompt_build", { prompt: "hello there" }, CONVO_A);
    await flush();

    // Switch-time sync fails; the user accepts force.
    mockImprove.mockImplementationOnce(async () => { throw new Error("improve 503"); });
    const tool = h.tools(CONVO_A).find((t) => t.name === "memory_switch_dataset")!;
    const res = (await tool.execute("c", { action: "switch", dataset: "proj-a", force: true })) as { details: { switched: boolean; previous: { synced: boolean } } };
    expect(res.details.switched).toBe(true);
    expect(res.details.previous.synced).toBe(false);
    mockImprove.mockClear();

    await h.emit("session_end", { sessionId: "s1", sessionKey: CONVO_A.sessionKey, messageCount: 2 }, CONVO_A);
    await flush(40);

    const calls = mockImprove.mock.calls.map((c) => c[0] as { datasetName: string; sessionIds: string[] });
    // Retired (old dataset, base session id) first, then the active switched session.
    expect(calls).toEqual([
      expect.objectContaining({ datasetName: "testds", sessionIds: ["open_claw_s1"] }),
      expect.objectContaining({ datasetName: "proj-a", sessionIds: ["open_claw_s1__2"] }),
    ]);
    // ...and it is flagged synced so it is not bridged again.
    const persisted = Object.values(overridesOnDisk)[0] as { retired: Array<{ sessionId: string; synced: boolean }> };
    expect(persisted.retired).toEqual([expect.objectContaining({ sessionId: "open_claw_s1", synced: true })]);
  });

  it("memory_search on a switched conversation searches the new dataset", async () => {
    const h = harness();
    await switchTo(h, "proj-a");
    mockRecall.mockClear();
    mockRecall.mockImplementation(async () => [{ id: "m1", text: "plan", score: 0.9 }]);

    const search = h.tools(CONVO_A).find((t) => t.name === "memory_search")!;
    const out = (await search.execute("c", { query: "plan", corpus: "memory" })) as { details: { results: Array<{ source: string }> } };
    expect(mockRecall.mock.calls[0][0].datasetIds).toEqual(["id-proj-a"]);
    expect(out.details.results[0].source).toBe("proj-a");
  });

  it("reset returns capture to the configured dataset and the base session id", async () => {
    const h = harness();
    await switchTo(h, "proj-a");
    const tool = h.tools(CONVO_A).find((t) => t.name === "memory_switch_dataset")!;
    expect(((await tool.execute("c", { action: "reset" })) as { details: { reset: boolean } }).details.reset).toBe(true);
    mockRememberEntry.mockClear();

    await h.emit("before_prompt_build", { prompt: "back to normal?" }, CONVO_A);
    await h.emit("llm_output", { assistantTexts: ["yes"] }, CONVO_A);
    await flush();
    const qa = mockRememberEntry.mock.calls.map((c) => c[0] as { datasetName: string; sessionId: string }).pop()!;
    expect(qa).toMatchObject({ datasetName: "testds", sessionId: "open_claw_s1" });
  });

  it("with multi-scope, only the agent scope is repointed; company stays shared", async () => {
    datasetState = { acme: "ds-company", "acme-agent-will": "ds-agent" };
    const h = harness({ companyDataset: "acme", agentDatasetPrefix: "acme-agent", recallScopes: ["agent", "company"] });
    await switchTo(h, "proj-a");
    mockRecall.mockClear();

    await h.emit("before_prompt_build", { prompt: "what is the plan?" }, CONVO_A);
    const graphLanes = mockRecall.mock.calls.map((c) => c[0]).filter((c) => !c.scope).map((c) => c.datasetIds[0]).sort();
    expect(graphLanes).toEqual(["ds-company", "id-proj-a"]);
  });
});
