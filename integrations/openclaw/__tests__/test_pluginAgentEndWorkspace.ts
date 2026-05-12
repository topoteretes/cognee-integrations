import plugin from "../src/plugin";
import { CogneeHttpClient } from "../src/client";
import { collectMemoryFiles } from "../src/files";
import {
  loadDatasetState,
  loadSyncIndex,
  loadScopedSyncIndexes,
} from "../src/persistence";
import { syncFiles, syncFilesScoped } from "../src/sync";
import { resolveConfig } from "../src/config";
import { buildMemoryFlushPlan } from "../src/flush-plan";
import type { CogneePluginConfig, MemoryFile, MemoryScope, ScopedSyncIndexes } from "../src/types";

// ---------------------------------------------------------------------------
// Module mocks
// ---------------------------------------------------------------------------

jest.mock("../src/client", () => ({ CogneeHttpClient: jest.fn() }));
jest.mock("../src/config", () => ({ resolveConfig: jest.fn() }));
jest.mock("../src/files", () => ({ collectMemoryFiles: jest.fn() }));
jest.mock("../src/persistence", () => ({
  loadDatasetState: jest.fn(),
  loadSyncIndex: jest.fn(),
  loadScopedSyncIndexes: jest.fn(),
  migrateLegacyIndex: jest.fn().mockResolvedValue(null),
  SCOPED_SYNC_INDEX_PATH: "/mock/.openclaw/scoped-sync-indexes.json",
  SYNC_INDEX_PATH: "/mock/.openclaw/sync-index.json",
}));
jest.mock("../src/sync", () => ({ syncFiles: jest.fn(), syncFilesScoped: jest.fn() }));
jest.mock("../src/flush-plan", () => ({
  buildMemoryFlushPlan: jest.fn().mockReturnValue({ relativePath: "memory/2026-04-03.md" }),
}));

// ---------------------------------------------------------------------------
// Typed mock references
// ---------------------------------------------------------------------------

const mockResolveConfig = resolveConfig as jest.Mock;
const mockCollectMemoryFiles = collectMemoryFiles as jest.Mock;
const mockLoadDatasetState = loadDatasetState as jest.Mock;
const mockLoadSyncIndex = loadSyncIndex as jest.Mock;
const mockLoadScopedSyncIndexes = loadScopedSyncIndexes as jest.Mock;
const mockSyncFiles = syncFiles as jest.Mock;
const mockSyncFilesScoped = syncFilesScoped as jest.Mock;
const mockHealth = jest.fn();

(CogneeHttpClient as jest.MockedClass<typeof CogneeHttpClient>).mockImplementation(
  () => ({ health: mockHealth, healthDetailed: jest.fn(), search: jest.fn(), fetchAPI: jest.fn() } as never),
);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function baseCfg(overrides: Partial<CogneePluginConfig> = {}): Required<CogneePluginConfig> {
  return {
    mode: "local",
    baseUrl: "http://test",
    apiKey: "key",
    username: "",
    password: "",
    datasetName: "test",
    companyDataset: "",
    userDatasetPrefix: "",
    agentDatasetPrefix: "",
    userId: "",
    agentId: "default",
    recallScopes: ["agent"] as MemoryScope[],
    defaultWriteScope: "agent" as MemoryScope,
    scopeRouting: [],
    autoIndex: true,
    autoRecall: false,
    enableSessions: false,
    persistSessionsAfterEnd: false,
    searchType: "FEELING_LUCKY",
    searchPrompt: "",
    deleteMode: "soft",
    maxResults: 6,
    minScore: 0,
    maxTokens: 512,
    autoCognify: false,
    autoMemify: false,
    requestTimeoutMs: 30_000,
    ingestionTimeoutMs: 300_000,
    recallInjectionPosition: "prependContext",
    ...overrides,
  } as Required<CogneePluginConfig>;
}

const makeFile = (path: string, hash: string): MemoryFile => ({
  path,
  absPath: `/workspace/${path}`,
  content: "content",
  hash,
});

type AgentEndHandler = (
  event: { success: boolean },
  ctx: { agentId?: string; workspaceDir?: string },
) => Promise<void>;

function createApi(cfgOverrides: Partial<CogneePluginConfig> = {}) {
  const cfg = baseCfg(cfgOverrides);
  mockResolveConfig.mockReturnValue(cfg);

  const eventHandlers: Record<string, AgentEndHandler> = {};

  const api = {
    id: "cognee-openclaw",
    name: "Memory (Cognee)",
    source: "test",
    config: { agents: { list: [], defaults: { workspace: undefined } } },
    pluginConfig: {},
    runtime: {
      config: {
        loadConfig: jest.fn().mockReturnValue({}),
        writeConfigFile: jest.fn().mockResolvedValue(undefined),
      },
    },
    logger: { info: jest.fn(), warn: jest.fn(), debug: jest.fn() },
    registerMemoryCapability: jest.fn(),
    registerCli: jest.fn(),
    // Immediately calls start() so resolveServiceReady() fires before the first await
    // inside runAutoSync — making serviceReady resolve synchronously.
    // health() rejects to prevent actual auto-sync from touching collectMemoryFiles.
    registerService: jest.fn().mockImplementation(
      (svc: { start: (ctx: { workspaceDir: string }) => void }) => {
        svc.start({ workspaceDir: "/startup-workspace" });
      },
    ),
    on: jest.fn().mockImplementation((event: string, handler: AgentEndHandler) => {
      eventHandlers[event] = handler;
    }),
  };

  plugin.register(api as never);

  return { api, cfg, eventHandlers };
}

// ---------------------------------------------------------------------------
// Plugin registration
// ---------------------------------------------------------------------------

describe("cognee-openclaw plugin registration", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockHealth.mockRejectedValue(new Error("offline"));
    mockLoadDatasetState.mockResolvedValue({});
    mockLoadSyncIndex.mockResolvedValue({ entries: {} });
    mockLoadScopedSyncIndexes.mockResolvedValue({});
    mockCollectMemoryFiles.mockResolvedValue([]);
    mockSyncFiles.mockResolvedValue({ added: 0, updated: 0, skipped: 0, errors: 0, deleted: 0 });
  });

  it("registers memory capability with buildMemoryFlushPlan as flushPlanResolver", () => {
    const { api } = createApi({ autoIndex: false });
    expect(api.registerMemoryCapability).toHaveBeenCalledTimes(1);
    const call = (api.registerMemoryCapability as jest.Mock).mock.calls[0][0];
    expect(call.flushPlanResolver).toBe(buildMemoryFlushPlan);
  });

  it("registered flushPlanResolver returns correct relativePath", () => {
    const { api } = createApi({ autoIndex: false });
    const call = (api.registerMemoryCapability as jest.Mock).mock.calls[0][0];
    const resolver = call.flushPlanResolver as (params?: { nowMs?: number }) => { relativePath: string } | null;
    expect(resolver({ nowMs: Date.UTC(2026, 3, 3, 0, 0, 0) })?.relativePath).toBe("memory/2026-04-03.md");
  });

  it("registers memory capability with a backend config resolver returning 'builtin'", () => {
    const { api } = createApi({ autoIndex: false });
    const call = (api.registerMemoryCapability as jest.Mock).mock.calls[0][0];
    expect(call.runtime.resolveMemoryBackendConfig()).toEqual({ backend: "builtin" });
  });

  it("does not register an agent_end handler when autoIndex is false", () => {
    const { eventHandlers } = createApi({ autoIndex: false });
    expect(eventHandlers["agent_end"]).toBeUndefined();
  });

  it("registers an agent_end handler when autoIndex is true", () => {
    const { eventHandlers } = createApi({ autoIndex: true });
    expect(typeof eventHandlers["agent_end"]).toBe("function");
  });
});

// ---------------------------------------------------------------------------
// agent_end: early-exit guard
// ---------------------------------------------------------------------------

describe("cognee-openclaw agent_end — early exit on failure", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockHealth.mockRejectedValue(new Error("offline"));
    mockLoadDatasetState.mockResolvedValue({ test: "ds1" });
    mockLoadSyncIndex.mockResolvedValue({ entries: {}, datasetId: "ds1" });
    mockLoadScopedSyncIndexes.mockResolvedValue({});
    mockCollectMemoryFiles.mockResolvedValue([]);
    mockSyncFiles.mockResolvedValue({ added: 0, updated: 0, skipped: 0, errors: 0, deleted: 0 });
  });

  it("does nothing when event.success is false", async () => {
    const { eventHandlers } = createApi();
    await eventHandlers["agent_end"]({ success: false }, { agentId: "default", workspaceDir: "/ws" });
    expect(mockCollectMemoryFiles).not.toHaveBeenCalled();
    expect(mockSyncFiles).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// agent_end: workspace resolution
// ---------------------------------------------------------------------------

describe("cognee-openclaw agent_end — workspace resolution", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockHealth.mockRejectedValue(new Error("offline"));
    mockLoadDatasetState.mockResolvedValue({ test: "ds1" });
    // Called once in stateReady and once inside agent_end for the fresh index reload
    mockLoadSyncIndex.mockResolvedValue({ entries: {}, datasetId: "ds1" });
    mockLoadScopedSyncIndexes.mockResolvedValue({});
    mockCollectMemoryFiles.mockResolvedValue([]);
    mockSyncFiles.mockResolvedValue({ added: 0, updated: 0, skipped: 0, errors: 0, deleted: 0 });
  });

  it("falls back to resolvedWorkspaceDir (set during startup)", async () => {
    const { eventHandlers } = createApi();
    // registerService mock called start({ workspaceDir: "/startup-workspace" })
    await eventHandlers["agent_end"]({ success: true }, { agentId: "default" });
    expect(mockCollectMemoryFiles).toHaveBeenCalledWith("/startup-workspace");
  });
});

// ---------------------------------------------------------------------------
// agent_end: single-scope sync
// ---------------------------------------------------------------------------

describe("cognee-openclaw agent_end — single-scope sync", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockHealth.mockRejectedValue(new Error("offline"));
    mockLoadDatasetState.mockResolvedValue({ test: "ds1" });
    mockLoadSyncIndex.mockResolvedValue({ entries: {}, datasetId: "ds1" });
    mockCollectMemoryFiles.mockResolvedValue([]);
    mockSyncFiles.mockResolvedValue({ added: 0, updated: 0, skipped: 0, errors: 0, deleted: 0 });
  });

  it("skips syncFiles when workspace has no files and index is empty", async () => {
    const { eventHandlers } = createApi();
    await eventHandlers["agent_end"]({ success: true }, { agentId: "default", workspaceDir: "/ws" });
    expect(mockSyncFiles).not.toHaveBeenCalled();
  });

  it("skips syncFiles when all workspace files are unchanged", async () => {
    mockLoadSyncIndex.mockResolvedValue({
      entries: { "memory/file.md": { hash: "h1", dataId: "id1" } },
      datasetId: "ds1",
    });
    mockCollectMemoryFiles.mockResolvedValue([makeFile("memory/file.md", "h1")]);

    const { eventHandlers } = createApi();
    await eventHandlers["agent_end"]({ success: true }, { agentId: "default", workspaceDir: "/ws" });
    expect(mockSyncFiles).not.toHaveBeenCalled();
  });

  it("calls syncFiles when a file hash has changed", async () => {
    mockLoadSyncIndex.mockResolvedValue({
      entries: { "memory/file.md": { hash: "old-hash", dataId: "id1" } },
      datasetId: "ds1",
    });
    mockCollectMemoryFiles.mockResolvedValue([makeFile("memory/file.md", "new-hash")]);
    mockSyncFiles.mockResolvedValue({ added: 0, updated: 1, skipped: 0, errors: 0, deleted: 0 });

    const { eventHandlers } = createApi();
    await eventHandlers["agent_end"]({ success: true }, { agentId: "default", workspaceDir: "/ws" });
    expect(mockSyncFiles).toHaveBeenCalledTimes(1);
  });

  it("calls syncFiles when a new file appears in the workspace", async () => {
    mockLoadSyncIndex.mockResolvedValue({ entries: {}, datasetId: "ds1" });
    mockCollectMemoryFiles.mockResolvedValue([makeFile("memory/new.md", "h1")]);
    mockSyncFiles.mockResolvedValue({ added: 1, updated: 0, skipped: 0, errors: 0, deleted: 0 });

    const { eventHandlers } = createApi();
    await eventHandlers["agent_end"]({ success: true }, { agentId: "default", workspaceDir: "/ws" });
    expect(mockSyncFiles).toHaveBeenCalledTimes(1);
  });

  it("calls syncFiles when an indexed file is deleted from the workspace", async () => {
    mockLoadSyncIndex.mockResolvedValue({
      entries: { "memory/gone.md": { hash: "h1", dataId: "id1" } },
      datasetId: "ds1",
    });
    // File no longer present in workspace
    mockCollectMemoryFiles.mockResolvedValue([]);
    mockSyncFiles.mockResolvedValue({ added: 0, updated: 0, skipped: 0, errors: 0, deleted: 1 });

    const { eventHandlers } = createApi();
    await eventHandlers["agent_end"]({ success: true }, { agentId: "default", workspaceDir: "/ws" });
    expect(mockSyncFiles).toHaveBeenCalledTimes(1);
  });

  it("passes only changed files as changedFiles but full file list as allFiles to syncFiles", async () => {
    mockLoadSyncIndex.mockResolvedValue({
      entries: {
        "memory/unchanged.md": { hash: "h-same", dataId: "id1" },
        "memory/changed.md": { hash: "old-hash", dataId: "id2" },
      },
      datasetId: "ds1",
    });
    const allFiles = [
      makeFile("memory/unchanged.md", "h-same"),
      makeFile("memory/changed.md", "new-hash"),
    ];
    mockCollectMemoryFiles.mockResolvedValue(allFiles);
    mockSyncFiles.mockResolvedValue({ added: 0, updated: 1, skipped: 0, errors: 0, deleted: 0 });

    const { eventHandlers } = createApi();
    await eventHandlers["agent_end"]({ success: true }, { agentId: "default", workspaceDir: "/ws" });

    const [, changedArg, allArg] = (mockSyncFiles as jest.Mock).mock.calls[0];
    expect(changedArg).toHaveLength(1);
    expect(changedArg[0].path).toBe("memory/changed.md");
    expect(allArg).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// agent_end: multi-scope sync
// ---------------------------------------------------------------------------

describe("cognee-openclaw agent_end — multi-scope sync", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockHealth.mockRejectedValue(new Error("offline"));
    mockLoadDatasetState.mockResolvedValue({});
    mockLoadScopedSyncIndexes.mockResolvedValue({});
    mockCollectMemoryFiles.mockResolvedValue([]);
    mockSyncFilesScoped.mockResolvedValue({ added: 0, updated: 0, skipped: 0, errors: 0, deleted: 0 });
  });

  it("skips syncFilesScoped when workspace has no files and scoped indexes are empty", async () => {
    const { eventHandlers } = createApi({ companyDataset: "acme-shared" });
    await eventHandlers["agent_end"]({ success: true }, { agentId: "default", workspaceDir: "/ws" });
    expect(mockSyncFilesScoped).not.toHaveBeenCalled();
  });

  it("calls syncFilesScoped when a file hash has changed in any scope", async () => {
    mockLoadScopedSyncIndexes.mockResolvedValue({
      agent: { entries: { "memory/tools.md": { hash: "old-hash", dataId: "id1" } }, datasetId: "ds-agent" },
    } as ScopedSyncIndexes);
    mockCollectMemoryFiles.mockResolvedValue([makeFile("memory/tools.md", "new-hash")]);
    mockSyncFilesScoped.mockResolvedValue({ added: 0, updated: 1, skipped: 0, errors: 0, deleted: 0 });

    const { eventHandlers } = createApi({ companyDataset: "acme-shared" });
    await eventHandlers["agent_end"]({ success: true }, { agentId: "default", workspaceDir: "/ws" });
    expect(mockSyncFilesScoped).toHaveBeenCalledTimes(1);
  });

  it("calls syncFilesScoped when a new file appears in the workspace", async () => {
    mockLoadScopedSyncIndexes.mockResolvedValue({} as ScopedSyncIndexes);
    mockCollectMemoryFiles.mockResolvedValue([makeFile("memory/new.md", "h1")]);
    mockSyncFilesScoped.mockResolvedValue({ added: 1, updated: 0, skipped: 0, errors: 0, deleted: 0 });

    const { eventHandlers } = createApi({ companyDataset: "acme-shared" });
    await eventHandlers["agent_end"]({ success: true }, { agentId: "default", workspaceDir: "/ws" });
    expect(mockSyncFilesScoped).toHaveBeenCalledTimes(1);
  });

  it("reloads scoped indexes from disk before checking for changes", async () => {
    mockCollectMemoryFiles.mockResolvedValue([]);

    const { eventHandlers } = createApi({ companyDataset: "acme-shared" });
    await eventHandlers["agent_end"]({ success: true }, { agentId: "default", workspaceDir: "/ws" });

    // loadScopedSyncIndexes called once in stateReady and once inside agent_end
    expect(mockLoadScopedSyncIndexes).toHaveBeenCalledTimes(2);
  });
});
