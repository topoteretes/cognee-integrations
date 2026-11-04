/**
 * Auto recall through the plugin's prompt hook and memory_switch_dataset
 * (SDK-1393): unpinned prompt recall names no dataset, an install with no
 * indexed dataset skips recall, a pick pins it, and `current` reports the mode.
 * The harness runs on the default dataset (auto recall); a non-default
 * configured datasetName pins recall unless the conversation picks auto.
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
  mockBreaker.openForSeconds.mockImplementation(async () => 0);
  mockRecall.mockImplementation(async () => []);
  (CogneeHttpClient as unknown as jest.Mock).mockImplementation(() => ({
    recall: mockRecall,
    health: jest.fn(async () => ({ status: "ok" })),
    listDatasets: jest.fn(async () => []),
    setApiKey: jest.fn(),
  }));
});

describe("prompt recall", () => {
  const graphCall = () => mockRecall.mock.calls.map((c) => c[0]).find((c) => c.scope?.includes("graph"))!;

  it("routes with no dataset ids when unpinned", async () => {
    const h = harness();
    await h.emit("before_prompt_build", PROMPT, CONVO);
    expect(graphCall().datasetIds).toEqual([]);
  });

  it("skips recall while the recall breaker is open", async () => {
    mockBreaker.openForSeconds.mockImplementation(async () => 42);
    const h = harness();
    await h.emit("before_prompt_build", PROMPT, CONVO);
    expect(mockRecall).not.toHaveBeenCalled();
  });

  it("skips recall on an empty install (no dataset with data), even unpinned", async () => {
    datasetState = {};
    const h = harness();
    await h.emit("before_prompt_build", PROMPT, CONVO);
    expect(mockRecall).not.toHaveBeenCalled();
    expect(h.logger.debug).toHaveBeenCalledWith(expect.stringContaining("skipping recall (no datasetIds)"));
  });

  it("sends the dataset's id once the conversation picks the dataset in use", async () => {
    const h = harness();
    const tool = h.tools(CONVO).find((t) => t.name === "memory_switch_dataset")!;
    await tool.execute("c", { action: "switch", dataset: "agent_sessions" });
    await h.emit("before_prompt_build", PROMPT, CONVO);
    expect(graphCall().datasetIds).toEqual(["ds-1"]);
  });
});

describe("memory_switch_dataset reports the recall mode in effect", () => {
  async function run(h: ReturnType<typeof harness>, params: Record<string, unknown>) {
    const tool = h.tools(CONVO).find((t) => t.name === "memory_switch_dataset")!;
    return ((await tool.execute("c", params)) as { details: Record<string, unknown> }).details;
  }

  it("unpinned: auto", async () => {
    expect(await run(harness(), { action: "current" })).toMatchObject({ recall: "auto" });
  });

  it("a pick pins, and action=auto unpins", async () => {
    const h = harness();
    await run(h, { action: "switch", dataset: "agent_sessions" });
    expect(await run(h, { action: "current" })).toMatchObject({ dataset: "agent_sessions", recall: "pinned" });
    const auto = await run(h, { action: "auto" });
    expect(auto).toMatchObject({ changed: true, recall: "auto" });
    expect(String(auto.note)).not.toMatch(/route yet|stays on/);
  });
});

describe("a non-default config datasetName pins recall", () => {
  const PINNED = { datasetName: "testds" };

  beforeEach(() => {
    datasetState = { agent_sessions: "ds-1", testds: "ds-2" };
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

  it("prompt recall and memory_search send the configured dataset's id", async () => {
    const h = harness(PINNED);
    expect((await promptGraph(h)).datasetIds).toEqual(["ds-2"]);
    const calls = await search(h);
    expect(calls.length).toBeGreaterThan(0);
    for (const c of calls) expect(c.datasetIds).toEqual(["ds-2"]);
  });

  it("the default dataset routes: no dataset ids", async () => {
    const h = harness();
    expect((await promptGraph(h)).datasetIds).toEqual([]);
    const calls = await search(h);
    expect(calls.map((c) => c.datasetIds)).toEqual([[]]);
  });

  it("current reports the config pin", async () => {
    const cur = await run(harness(PINNED), { action: "current" });
    expect(cur).toMatchObject({ dataset: "testds", recall: "pinned", switched: false });
    expect(cur.note).toMatch(/pinned by config datasetName "testds"/);
  });

  it("action=auto routes this conversation and survives a restart", async () => {
    const h = harness(PINNED);
    expect(await run(h, { action: "auto" })).toMatchObject({ changed: true, recall: "auto", dataset: "testds" });
    expect((await promptGraph(h)).datasetIds).toEqual([]);
    expect((await search(h)).map((c) => c.datasetIds)).toEqual([[]]);

    // Gateway restart: a fresh store loads the saved override.
    await DatasetSwitchStore.shared().flush();
    expect(Object.values(savedOverrides)).toContainEqual(expect.objectContaining({ dataset: "testds", recall: "auto" }));
    DatasetSwitchStore.resetShared();
    const restarted = harness(PINNED);
    expect((await promptGraph(restarted)).datasetIds).toEqual([]);
    expect(await run(restarted, { action: "current" })).toMatchObject({ recall: "auto" });
  });
});
