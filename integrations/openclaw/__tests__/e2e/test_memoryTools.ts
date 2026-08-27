/**
 * memory_search / memory_get registered through the plugin: the factory is
 * handed to `api.registerTool` with both names declared, tool instances see the
 * calling agent's datasets, the breaker gates them, and `memoryTools: false`
 * registers nothing. The manifest must declare the same names in
 * `contracts.tools` or the host refuses the registration.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import plugin from "../../src/plugin";
import { CogneeHttpClient } from "../../src/client";
import { createPluginApi, serverMock } from "../../test-utils/fakeApi";

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

const mockRecall = jest.fn(async (_params: unknown): Promise<unknown[]> => []);

type SearchDetails = { results: Array<{ reference: string; text: string; source: string; scope: string }>; disabled?: boolean; error?: string };

beforeEach(() => {
  jest.clearAllMocks();
  datasetState = { testds: "ds-1" };
  mockBreaker.openForSeconds.mockImplementation(async () => 0);
  mockRecall.mockImplementation(async () => []);
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
  void serverMock;
});

describe("tool registration", () => {
  it("registers a factory under all contract names", () => {
    const { registerTool, tools } = createPluginApi(plugin);
    expect(registerTool).toHaveBeenCalledTimes(1);
    expect(registerTool.mock.calls[0][1]).toEqual({ names: ["memory_search", "memory_get", "memory_forget", "memory_switch_dataset"] });
    expect(tools({ agentId: "will" }).map((t) => t.name)).toEqual(["memory_search", "memory_get", "memory_forget", "memory_switch_dataset"]);
  });

  it("leaves optional tools out when their flags are false", () => {
    const { registerTool, tools } = createPluginApi(plugin, { memoryForgetTool: false, datasetSwitchTool: false });
    expect(registerTool.mock.calls[0][1]).toEqual({ names: ["memory_search", "memory_get"] });
    expect(tools({ agentId: "will" }).map((t) => t.name)).toEqual(["memory_search", "memory_get"]);
  });

  it("registers nothing when memoryTools is false", () => {
    const { registerTool } = createPluginApi(plugin, { memoryTools: false });
    expect(registerTool).not.toHaveBeenCalled();
  });

  it("still registers when autoRecall is off — the rails are independent", () => {
    const { registerTool } = createPluginApi(plugin, { autoRecall: false });
    expect(registerTool).toHaveBeenCalledTimes(1);
  });

  it("declares the same tool names in the manifest contracts", () => {
    const manifest = JSON.parse(readFileSync(join(__dirname, "..", "..", "openclaw.plugin.json"), "utf-8"));
    expect(manifest.contracts.tools.sort()).toEqual(["memory_forget", "memory_get", "memory_search", "memory_switch_dataset"]);
    expect(manifest.configSchema.properties.memoryForgetTool.type).toBe("boolean");
    expect(manifest.configSchema.properties.datasetSwitchTool.type).toBe("boolean");
    expect(manifest.configSchema.properties.memoryTools.type).toBe("boolean");
    expect(manifest.configSchema.properties.improveOnSessionEnd.type).toBe("boolean");
  });
});

describe("memory_search through the plugin", () => {
  it("searches the agent's dataset and hands back resolvable references", async () => {
    mockRecall.mockImplementation(async () => [
      { id: "m1", text: "User prefers dark mode", score: 0.9, metadata: { source: "MEMORY.md" } },
    ]);
    const { tools } = createPluginApi(plugin);
    const [searchTool, getTool] = tools({ agentId: "will", sessionId: "s1" });

    const res = await searchTool.execute("c1", { query: "theme?", corpus: "memory" });
    const details = (res as { details: SearchDetails }).details;
    expect(details.results).toHaveLength(1);
    expect(details.results[0]).toMatchObject({ text: "User prefers dark mode", source: "MEMORY.md", scope: "graph" });
    expect(mockRecall).toHaveBeenCalledWith(expect.objectContaining({ datasetIds: ["ds-1"], queryText: "theme?", timeoutMs: expect.any(Number) }));

    const got = await getTool.execute("c2", { path: details.results[0].reference });
    expect((got as { details: { text: string } }).details.text).toBe("User prefers dark mode");
  });

  it("uses the multi-scope datasets, labelling provenance by scope", async () => {
    datasetState = { "acme": "ds-company", "acme-agent-will": "ds-agent" };
    mockRecall.mockImplementation(async (p: unknown) => {
      const ids = (p as { datasetIds: string[] }).datasetIds;
      return [{ id: `hit-${ids[0]}`, text: `from ${ids[0]}`, score: 0.7 }];
    });
    const { tools } = createPluginApi(plugin, {
      companyDataset: "acme",
      agentDatasetPrefix: "acme-agent",
      recallScopes: ["agent", "company"],
    });
    const [searchTool] = tools({ agentId: "will" });
    const details = ((await searchTool.execute("c", { query: "q", corpus: "memory" })) as { details: SearchDetails }).details;
    expect(details.results.map((h) => h.source).sort()).toEqual(["agent", "company"]);
  });

  it("reports disabled while the breaker is open and never hits the server", async () => {
    mockBreaker.openForSeconds.mockImplementation(async () => 30);
    const { tools } = createPluginApi(plugin);
    const [searchTool] = tools({ agentId: "will" });
    const details = ((await searchTool.execute("c", { query: "q" })) as { details: SearchDetails }).details;
    expect(details.disabled).toBe(true);
    expect(mockRecall).not.toHaveBeenCalled();
  });

  it("reports disabled (not a throw) when the server is unreachable", async () => {
    mockRecall.mockImplementation(async () => { throw new Error("fetch failed"); });
    const { tools } = createPluginApi(plugin);
    const [searchTool] = tools({ agentId: "will" });
    const details = ((await searchTool.execute("c", { query: "q", corpus: "memory" })) as { details: SearchDetails }).details;
    expect(details).toMatchObject({ results: [], disabled: true, error: "Error: fetch failed" });
  });
});
