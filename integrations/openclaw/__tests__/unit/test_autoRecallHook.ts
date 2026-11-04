/**
 * Auto recall through the plugin's prompt hook and memory_switch_dataset
 * (SDK-1393): an install with no indexed dataset skips recall, and the
 * configured and per-conversation recall modes decide the dataset ids sent.
 * The harness runs on the default dataset (auto recall); the config's
 * recallMode "pinned" pins recall unless the conversation picks auto, and a
 * datasetName alone never pins.
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
/** The overrides "on disk": survives a fresh harness (a gateway restart). */
let savedOverrides: Record<string, unknown> = {};
jest.mock("../../src/persistence", () => ({
  loadDatasetState: jest.fn(async () => ({ ...datasetState })),
  saveDatasetState: jest.fn(async (s: Record<string, string>) => { datasetState = { ...s }; }),
  DATASET_OVERRIDES_PATH: "/tmp/dataset-overrides.json",
  loadDatasetOverrides: jest.fn(async () => JSON.parse(JSON.stringify(savedOverrides))),
  saveDatasetOverrides: jest.fn(async (o: Record<string, unknown>) => { savedOverrides = JSON.parse(JSON.stringify(o)); }),
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

type RecallParams = { datasetIds: string[]; scope?: string[] };
const mockRecall = jest.fn(async (_p: RecallParams): Promise<unknown[]> => []);

const CONVO = { agentId: "will", sessionId: "s1", sessionKey: "agent:will:tg-1" };
const PROMPT = { prompt: "what is the plan for the launch?" };

function harness(extra: Record<string, unknown> = {}) {
  return createPluginApi(plugin, { autoRecall: true, enableSessions: false, captureSession: false, memorySteer: false, minScore: 0, datasetName: "agent_sessions", ...extra });
}

beforeEach(() => {
  jest.clearAllMocks();
  DatasetSwitchStore.resetShared();
  datasetState = { agent_sessions: "ds-1" };
  savedOverrides = {};
  mockRecall.mockImplementation(async () => []);
  (CogneeHttpClient as unknown as jest.Mock).mockImplementation(() => ({
    recall: mockRecall,
    health: jest.fn(async () => ({ status: "ok" })),
    listDatasets: jest.fn(async () => []),
    setApiKey: jest.fn(),
  }));
});

describe("prompt recall", () => {
  it("skips recall on an empty install (no dataset with data), even unpinned", async () => {
    datasetState = {};
    const h = harness();
    await h.emit("before_prompt_build", PROMPT, CONVO);
    expect(mockRecall).not.toHaveBeenCalled();
    expect(h.logger.debug).toHaveBeenCalledWith(expect.stringContaining("skipping recall (no datasetIds)"));
  });
});

describe("configured recallMode", () => {
  const NAMED = { datasetName: "testds" };
  const PINNED = { datasetName: "testds", recallMode: "pinned" };

  beforeEach(() => {
    datasetState = { agent_sessions: "ds-1", testds: "ds-2", other: "ds-3" };
  });

  async function run(h: ReturnType<typeof harness>, params: Record<string, unknown>) {
    const tool = h.tools(CONVO).find((t) => t.name === "memory_switch_dataset")!;
    return ((await tool.execute("c", params)) as { details: Record<string, unknown> }).details;
  }

  async function search(h: ReturnType<typeof harness>) {
    mockRecall.mockClear();
    const tool = h.tools(CONVO).find((t) => t.name === "memory_search")!;
    await tool.execute("c", { query: "launch plan" });
    return mockRecall.mock.calls.map((c) => c[0]);
  }

  async function promptGraph(h: ReturnType<typeof harness>) {
    mockRecall.mockClear();
    await h.emit("before_prompt_build", PROMPT, CONVO);
    return mockRecall.mock.calls.map((c) => c[0]).find((c) => c.scope?.includes("graph"))!;
  }

  it("a non-default datasetName alone routes: no dataset ids", async () => {
    const h = harness(NAMED);
    expect((await promptGraph(h)).datasetIds).toEqual([]);
    expect((await search(h)).map((c) => c.datasetIds)).toEqual([[]]);
    expect(await run(h, { action: "current" })).toMatchObject({ dataset: "testds", recall: "auto" });
  });

  it("recallMode pinned: prompt recall and memory_search send the configured dataset's id", async () => {
    const h = harness(PINNED);
    expect((await promptGraph(h)).datasetIds).toEqual(["ds-2"]);
    const calls = await search(h);
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) expect(c.datasetIds).toEqual(["ds-2"]);
    const cur = await run(h, { action: "current" });
    expect(cur).toMatchObject({ dataset: "testds", recall: "pinned", reason: "configured_pin", switched: false });
    expect(cur.note).toMatch(/recallMode "pinned"/);
  });

  it("action=auto routes this conversation and survives a restart", async () => {
    const h = harness(PINNED);
    expect(await run(h, { action: "auto" })).toMatchObject({ changed: true, recall: "auto", dataset: "testds" });
    expect((await promptGraph(h)).datasetIds).toEqual([]);
    expect((await search(h)).map((c) => c.datasetIds)).toEqual([[]]);

    // Gateway restart: a fresh store loads the saved choice — a mode only.
    await DatasetSwitchStore.shared().flush();
    expect(Object.keys(savedOverrides).sort()).toEqual(["key:agent:will:tg-1", "sid:s1"]);
    for (const v of Object.values(savedOverrides)) expect(v).toEqual({ recall: "auto", chosenAt: expect.any(String) });
    DatasetSwitchStore.resetShared();
    const restarted = harness(PINNED);
    expect((await promptGraph(restarted)).datasetIds).toEqual([]);
    expect(await run(restarted, { action: "current" })).toMatchObject({ recall: "auto" });
  });

  // Picking the dataset in use is the same as action=pin.
  it.each([{ action: "pin" }, { action: "switch", dataset: "testds" }])("%o keeps recall pinned, but saves and recall follow a later datasetName change", async (pick) => {
    const h = harness(NAMED);
    expect(await run(h, pick)).toMatchObject({ action: pick.action, changed: true, recall: "pinned", reason: "dataset_picked", dataset: "testds" });
    expect((await promptGraph(h)).datasetIds).toEqual(["ds-2"]);

    await DatasetSwitchStore.shared().flush();
    DatasetSwitchStore.resetShared();
    const moved = harness({ datasetName: "other" });
    expect(await run(moved, { action: "current" })).toMatchObject({ dataset: "other", recall: "pinned", reason: "dataset_picked", switched: false });
    expect((await promptGraph(moved)).datasetIds).toEqual(["ds-3"]);
  });
});
