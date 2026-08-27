/**
 * Code graph through the plugin: `index-repo` submits the repository with
 * content_type=code, records the dataset in the registry and can wait on the
 * pipeline; the code recall lane fires only for identifier-bearing prompts
 * once a repo is registered; memory_code_search defaults to that dataset.
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
jest.mock("../../src/breaker", () => ({
  RecallBreaker: jest.fn(() => ({ openForSeconds: async () => 0, recordFailure: async () => {}, recordSuccess: async () => {} })),
  isBreakerError: () => false,
}));

let datasetState: Record<string, string> = {};
let codeGraphsOnDisk: Record<string, unknown> = {};
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
  loadDatasetOverrides: jest.fn(async () => ({})),
  saveDatasetOverrides: jest.fn(async () => {}),
  loadCodeGraphs: jest.fn(async () => ({ ...codeGraphsOnDisk })),
  saveCodeGraphs: jest.fn(async (d: Record<string, unknown>) => { codeGraphsOnDisk = { ...d }; }),
  UPDATE_CHECK_PATH: "/tmp/update-check.json",
  SYNC_INDEX_PATH: "/tmp/sync-index.json",
}));

type RecallParams = { datasetIds: string[]; scope?: string[]; codeQuery?: Record<string, unknown>; queryText: string };
const mockRecall = jest.fn(async (_p: RecallParams): Promise<unknown[]> => []);
const mockIndexRepository = jest.fn(async (_p: unknown) => ({ dataset_id: "id-code", pipeline_run_id: "run-1" }));
const mockPipelineStatus = jest.fn(async () => "completed");

const CODE_FACT = { id: "f1", text: "UserService.get -> Database.query", score: 1, source: "code" };
const GRAPH_HIT = { id: "g1", text: "graph memory", score: 0.9, source: "graph" };

let logSpy: jest.SpyInstance;
let exitSpy: jest.SpyInstance;
beforeEach(() => {
  jest.clearAllMocks();
  datasetState = { testds: "ds-1" };
  codeGraphsOnDisk = {};
  mockRecall.mockImplementation(async (p) => (p.scope?.includes("code") ? [CODE_FACT] : [GRAPH_HIT]));
  mockIndexRepository.mockImplementation(async () => ({ dataset_id: "id-code", pipeline_run_id: "run-1" }));
  mockPipelineStatus.mockImplementation(async () => "completed");
  (CogneeHttpClient as unknown as jest.Mock).mockImplementation(() => ({
    recall: mockRecall,
    indexRepository: mockIndexRepository,
    pipelineStatus: mockPipelineStatus,
    ensureDataset: jest.fn(async (name: string) => `id-${name}`),
    registerAgent: jest.fn(async () => ({ ok: true, connectionId: "c1" })),
    unregisterAgent: jest.fn(async () => ({ ok: true, activeAgents: 0 })),
    health: jest.fn(async () => ({ status: "ok" })),
    listDatasets: jest.fn(async () => []),
    setApiKey: jest.fn(),
  }));
  logSpy = jest.spyOn(console, "log").mockImplementation((...a: unknown[]) => { if (process.env.DEBUG_LINES) process.stderr.write(a.join(" ") + "\n"); });
  exitSpy = jest.spyOn(process, "exit").mockImplementation(((code?: number) => { throw new Error(`process.exit(${code})`); }) as never);
});
afterEach(() => { logSpy.mockRestore(); exitSpy.mockRestore(); });

function lines(): string[] { return logSpy.mock.calls.map((c) => String(c[0])); }

async function recallInjection(h: ReturnType<typeof createPluginApi>, prompt: string) {
  const handlers = (h.api.on as jest.Mock).mock.calls.filter((c) => c[0] === "before_prompt_build").map((c) => c[1]);
  let out: Record<string, string> | undefined;
  for (const fn of handlers) {
    const r = await fn({ prompt }, { agentId: "will" });
    if (r !== undefined) out = r as Record<string, string>;
  }
  return out?.prependContext ?? "";
}

describe("openclaw cognee index-repo", () => {
  it("submits the repo as a code graph, caches the dataset id, registers it, and waits for the pipeline", async () => {
    const h = createPluginApi(plugin);
    await expect(h.runCli("index-repo", { wait: "5" }, "https://github.com/org/repo.git")).rejects.toThrow(/process\.exit\(0\)/);

    expect(mockIndexRepository).toHaveBeenCalledWith({
      datasetName: expect.stringMatching(/^codebase-repo-[0-9a-f]{8}$/),
      repository: "https://github.com/org/repo.git",
      indexVectors: false,
      runInBackground: true,
    });
    const dataset = (mockIndexRepository.mock.calls[0][0] as { datasetName: string }).datasetName;
    expect(datasetState[dataset]).toBe("id-code");
    expect(mockPipelineStatus).toHaveBeenCalledWith("id-code", "code_graph_pipeline");
    expect(Object.keys(codeGraphsOnDisk)).toEqual([dataset]);
    expect(codeGraphsOnDisk[dataset]).toMatchObject({ dataset, kind: "url", spec: "https://github.com/org/repo.git", lastStatus: "completed" });
    expect(lines().some((l) => l.startsWith("Code graph indexing submitted"))).toBe(true);
    expect(lines().some((l) => l.startsWith("Code graph ready"))).toBe(true);
  });

  it("honours --dataset and --index-vectors, and explains a 400 as a server-version problem", async () => {
    const h = createPluginApi(plugin);
    await expect(h.runCli("index-repo", { dataset: "my-code", indexVectors: true }, "https://x/y")).rejects.toThrow(/process\.exit\(0\)/);
    expect(mockIndexRepository).toHaveBeenCalledWith(expect.objectContaining({ datasetName: "my-code", indexVectors: true }));

    mockIndexRepository.mockImplementation(async () => { throw new Error("HTTP (400) content_type unsupported"); });
    const h2 = createPluginApi(plugin);
    await expect(h2.runCli("index-repo", {}, "https://x/z")).rejects.toThrow(/process\.exit\(1\)/);
    expect(lines().some((l) => /requires Cognee >= 1\.5\.3/.test(l))).toBe(true);
  });
});

describe("code recall lane", () => {
  it("is silent without a registered code graph, even for identifier prompts", async () => {
    const h = createPluginApi(plugin, { autoRecall: true, enableSessions: false });
    const text = await recallInjection(h, "what calls `UserService`?");
    expect(mockRecall.mock.calls.some((c) => c[0].scope?.includes("code"))).toBe(false);
    expect(text).not.toContain("<code_graph>");
  });

  it("fires only for identifier-bearing prompts once a repo is registered, and appends a <code_graph> block", async () => {
    codeGraphsOnDisk = { "codebase-repo-1": { dataset: "codebase-repo-1", datasetId: "id-code", spec: "/r", canonical: "/r", kind: "path", indexVectors: false, indexedAt: "2026-08-27T00:00:00Z" } };
    const h = createPluginApi(plugin, { autoRecall: true, enableSessions: false });

    const plain = await recallInjection(h, "what did we decide about the rollout?");
    expect(mockRecall.mock.calls.some((c) => c[0].scope?.includes("code"))).toBe(false);
    expect(plain).toContain("graph memory");

    mockRecall.mockClear();
    const text = await recallInjection(h, "what calls `UserService` and does it break?");
    const lane = mockRecall.mock.calls.map((c) => c[0]).find((c) => c.scope?.includes("code"))!;
    expect(lane).toMatchObject({ datasetIds: ["id-code"], queryText: "UserService", codeQuery: { operation: "query_facts", name: "UserService", limit: 5 } });
    expect(text.indexOf("<graph_memory>")).toBeLessThan(text.indexOf("<code_graph>"));
    expect(text).toContain("UserService.get -> Database.query");
  });

  it("uses codeDatasets from config when nothing is registered locally, and can be disabled", async () => {
    datasetState = { testds: "ds-1", "codebase-remote": "id-remote" };
    const h = createPluginApi(plugin, { autoRecall: true, enableSessions: false, codeDatasets: ["codebase-remote"] });
    await recallInjection(h, "look at `process_payment`");
    expect(mockRecall.mock.calls.find((c) => c[0].scope?.includes("code"))![0].datasetIds).toEqual(["id-remote"]);

    mockRecall.mockClear();
    const off = createPluginApi(plugin, { autoRecall: true, enableSessions: false, codeDatasets: ["codebase-remote"], codeGraphRecall: false });
    await recallInjection(off, "look at `process_payment`");
    expect(mockRecall.mock.calls.some((c) => c[0].scope?.includes("code"))).toBe(false);
  });
});

describe("memory_code_search through the plugin", () => {
  it("defaults to the single registered code graph and forwards the structured query", async () => {
    codeGraphsOnDisk = { "codebase-repo-1": { dataset: "codebase-repo-1", datasetId: "id-code", spec: "/r", canonical: "/r", kind: "path", indexVectors: false, indexedAt: "2026-08-27T00:00:00Z" } };
    const h = createPluginApi(plugin);
    const tool = h.tools({ agentId: "will" }).find((t) => t.name === "memory_code_search")!;
    const out = (await tool.execute("c", { query: "process_payment", operation: "impact_analysis" })) as { details: { dataset: string; results: Array<{ text: string }> } };
    expect(mockRecall).toHaveBeenCalledWith(expect.objectContaining({ datasetIds: ["id-code"], scope: ["code"], codeQuery: { operation: "impact_analysis", targets: ["process_payment"] } }));
    expect(out.details.dataset).toBe("codebase-repo-1");
    expect(out.details.results[0].text).toBe("UserService.get -> Database.query");
  });
});
