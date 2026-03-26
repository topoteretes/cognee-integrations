import { CogneeHttpClient } from "../src/client";
import { syncFiles, syncFilesScoped, _setPollInterval } from "../src/sync";
import { matchGlob, routeFileToScope, datasetNameForScope, isMultiScopeEnabled } from "../src/scope";
import type { MemoryFile, SyncIndex, CogneePluginConfig, ScopedSyncIndexes, MemoryScope, ScopeRoute } from "../src/types";
import { homedir } from "node:os";
import { join } from "node:path";
import { promises as fs } from "node:fs";

jest.mock("node:fs", () => ({
  promises: {
    readFile: jest.fn(),
    writeFile: jest.fn(),
    mkdir: jest.fn(),
  },
}));

const mockFs = fs as jest.Mocked<typeof fs>;
const SYNC_INDEX_PATH = join(homedir(), ".openclaw", "memory", "cognee", "sync-index.json");
const STATE_PATH = join(homedir(), ".openclaw", "memory", "cognee", "datasets.json");
const SCOPED_SYNC_INDEX_PATH = join(homedir(), ".openclaw", "memory", "cognee", "scoped-sync-indexes.json");

// Mock CogneeHttpClient
jest.mock("../src/client", () => ({
  CogneeHttpClient: jest.fn(),
}));

const mockAdd = jest.fn();
const mockUpdate = jest.fn();
const mockDelete = jest.fn();
const mockCognify = jest.fn();
const mockMemify = jest.fn();
const mockDatasetStatus = jest.fn();

(CogneeHttpClient as jest.MockedClass<typeof CogneeHttpClient>).mockImplementation(() => ({
  add: mockAdd,
  update: mockUpdate,
  delete: mockDelete,
  cognify: mockCognify,
  memify: mockMemify,
  datasetStatus: mockDatasetStatus,
} as any));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function baseCfg(overrides: Partial<CogneePluginConfig> = {}): Required<CogneePluginConfig> {
  return {
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
    recallScopes: ["agent", "user", "company"] as MemoryScope[],
    defaultWriteScope: "agent" as MemoryScope,
    scopeRouting: [
      { pattern: "memory/company/**", scope: "company" as MemoryScope },
      { pattern: "memory/company/*", scope: "company" as MemoryScope },
      { pattern: "memory/user/**", scope: "user" as MemoryScope },
      { pattern: "memory/user/*", scope: "user" as MemoryScope },
      { pattern: "memory/**", scope: "agent" as MemoryScope },
      { pattern: "memory/*", scope: "agent" as MemoryScope },
      { pattern: "MEMORY.md", scope: "agent" as MemoryScope },
    ],
    enableSessions: true,
    persistSessionsAfterEnd: true,
    searchPrompt: "",
    searchType: "FEELING_LUCKY",
    deleteMode: "soft",
    maxResults: 6,
    minScore: 0,
    maxTokens: 512,
    autoRecall: true,
    autoIndex: true,
    autoCognify: true,
    autoMemify: false,
    requestTimeoutMs: 30000,
    ingestionTimeoutMs: 300000,
    enablePermissionGrants: false,
    grantReadUserIds: [],
    permissionEndpointPath: "/api/v1/datasets/permissions",
    ...overrides,
  } as Required<CogneePluginConfig>;
}

const createFile = (path: string, content: string, hash?: string): MemoryFile => ({
  path,
  absPath: `/workspace/${path}`,
  content,
  hash: hash || `hash-${content}`,
});

// ---------------------------------------------------------------------------
// matchGlob tests (Fix #3: proper glob support)
// ---------------------------------------------------------------------------

describe("matchGlob", () => {
  it("matches exact file", () => {
    expect(matchGlob("MEMORY.md", "MEMORY.md")).toBe(true);
    expect(matchGlob("MEMORY.md", "memory/foo.md")).toBe(false);
  });

  it("matches single-level wildcard", () => {
    expect(matchGlob("memory/*", "memory/foo.md")).toBe(true);
    expect(matchGlob("memory/*", "memory/sub/foo.md")).toBe(false);
  });

  it("matches double-star glob", () => {
    expect(matchGlob("memory/company/**", "memory/company/foo.md")).toBe(true);
    expect(matchGlob("memory/company/**", "memory/company/sub/foo.md")).toBe(true);
    expect(matchGlob("memory/company/**", "memory/user/foo.md")).toBe(false);
  });

  it("matches nested patterns", () => {
    expect(matchGlob("memory/**", "memory/foo.md")).toBe(true);
    expect(matchGlob("memory/**", "memory/company/foo.md")).toBe(true);
    expect(matchGlob("memory/**", "memory/user/deep/nested/foo.md")).toBe(true);
  });

  it("matches single-char wildcard (?)", () => {
    expect(matchGlob("memory/?.md", "memory/a.md")).toBe(true);
    expect(matchGlob("memory/?.md", "memory/ab.md")).toBe(false);
  });

  it("matches character classes [abc]", () => {
    expect(matchGlob("memory/[abc].md", "memory/a.md")).toBe(true);
    expect(matchGlob("memory/[abc].md", "memory/d.md")).toBe(false);
  });

  it("handles backslash paths (Windows normalization)", () => {
    expect(matchGlob("memory/company/**", "memory\\company\\foo.md")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// routeFileToScope tests
// ---------------------------------------------------------------------------

describe("routeFileToScope", () => {
  const routes: ScopeRoute[] = [
    { pattern: "memory/company/**", scope: "company" },
    { pattern: "memory/company/*", scope: "company" },
    { pattern: "memory/user/**", scope: "user" },
    { pattern: "memory/user/*", scope: "user" },
    { pattern: "memory/**", scope: "agent" },
    { pattern: "memory/*", scope: "agent" },
    { pattern: "MEMORY.md", scope: "agent" },
  ];

  it("routes company files to company scope", () => {
    expect(routeFileToScope("memory/company/policy.md", routes, "agent")).toBe("company");
    expect(routeFileToScope("memory/company/sub/deep.md", routes, "agent")).toBe("company");
  });

  it("routes user files to user scope", () => {
    expect(routeFileToScope("memory/user/prefs.md", routes, "agent")).toBe("user");
    expect(routeFileToScope("memory/user/feedback/item.md", routes, "agent")).toBe("user");
  });

  it("routes other memory files to agent scope", () => {
    expect(routeFileToScope("memory/tools.md", routes, "agent")).toBe("agent");
    expect(routeFileToScope("MEMORY.md", routes, "agent")).toBe("agent");
  });

  it("uses default scope for unmatched paths", () => {
    expect(routeFileToScope("other/file.md", routes, "user")).toBe("user");
  });
});

// ---------------------------------------------------------------------------
// datasetNameForScope tests
// ---------------------------------------------------------------------------

describe("datasetNameForScope", () => {
  it("uses companyDataset when configured", () => {
    expect(datasetNameForScope("company", baseCfg({ companyDataset: "acme-shared" }))).toBe("acme-shared");
  });

  it("falls back to datasetName-company", () => {
    expect(datasetNameForScope("company", baseCfg({ companyDataset: "" }))).toBe("test-company");
  });

  it("uses userDatasetPrefix with userId", () => {
    expect(datasetNameForScope("user", baseCfg({ userDatasetPrefix: "proj-user", userId: "alice" }))).toBe("proj-user-alice");
  });

  it("uses agentDatasetPrefix with agentId", () => {
    expect(datasetNameForScope("agent", baseCfg({ agentDatasetPrefix: "proj-agent", agentId: "coder" }))).toBe("proj-agent-coder");
  });
});

// ---------------------------------------------------------------------------
// isMultiScopeEnabled tests
// ---------------------------------------------------------------------------

describe("isMultiScopeEnabled", () => {
  it("returns false when no scope-specific config", () => {
    expect(isMultiScopeEnabled(baseCfg())).toBe(false);
  });
  it("returns true when companyDataset is set", () => {
    expect(isMultiScopeEnabled(baseCfg({ companyDataset: "acme" }))).toBe(true);
  });
  it("returns true when userDatasetPrefix is set", () => {
    expect(isMultiScopeEnabled(baseCfg({ userDatasetPrefix: "proj-user" }))).toBe(true);
  });
  it("returns true when agentDatasetPrefix is set", () => {
    expect(isMultiScopeEnabled(baseCfg({ agentDatasetPrefix: "proj-agent" }))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// syncFiles tests
// ---------------------------------------------------------------------------

describe("syncFiles", () => {
  let client: CogneeHttpClient;
  let cfg: Required<CogneePluginConfig>;
  let logger: { info?: jest.Mock; warn?: jest.Mock };

  beforeEach(() => {
    jest.clearAllMocks();
    mockFs.readFile.mockImplementation(async (path) => {
      if (path === SYNC_INDEX_PATH) return JSON.stringify({ entries: {} });
      if (path === STATE_PATH) return JSON.stringify({});
      if (path === SCOPED_SYNC_INDEX_PATH) return JSON.stringify({});
      throw new Error(`Unexpected file read: ${path}`);
    });
    mockFs.writeFile.mockResolvedValue(undefined);
    mockFs.mkdir.mockResolvedValue(undefined);
    client = new CogneeHttpClient("http://test", "key");
    cfg = baseCfg();
    logger = { info: jest.fn(), warn: jest.fn() };
  });

  it("adds new file and updates syncIndex", async () => {
    const files = [createFile("new.md", "content")];
    const syncIndex: SyncIndex = { entries: {} };
    mockAdd.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id1" });

    const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

    expect(result).toEqual({ added: 1, updated: 0, skipped: 0, errors: 0, deleted: 0, datasetId: "ds1" });
    expect(syncIndex.entries["new.md"]).toEqual({ hash: "hash-content", dataId: "id1" });
    expect(mockCognify).toHaveBeenCalledWith({ datasetIds: ["ds1"] });
  });

  it("updates changed file with dataId", async () => {
    const files = [createFile("existing.md", "new content")];
    const syncIndex: SyncIndex = { entries: { "existing.md": { hash: "old-hash", dataId: "id1" } }, datasetId: "ds1" };
    mockUpdate.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id1" });

    const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

    expect(result).toEqual({ added: 0, updated: 1, skipped: 0, errors: 0, deleted: 0, datasetId: "ds1" });
    expect(mockCognify).not.toHaveBeenCalled();
  });

  it("falls back to add when update fails with 404", async () => {
    const files = [createFile("existing.md", "new content")];
    const syncIndex: SyncIndex = { entries: { "existing.md": { hash: "old-hash", dataId: "id1" } } };
    mockUpdate.mockRejectedValue(new Error("404 Not found"));
    mockAdd.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id2" });

    const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

    expect(result.added).toBe(1);
    expect(syncIndex.entries["existing.md"]?.dataId).toBe("id2");
  });

  it("handles update failure without fallback", async () => {
    const files = [createFile("existing.md", "new content")];
    const syncIndex: SyncIndex = { entries: { "existing.md": { hash: "old-hash", dataId: "id1" } }, datasetId: "ds1" };
    mockUpdate.mockRejectedValue(new Error("500 Internal error"));

    const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

    expect(result.errors).toBe(1);
    expect(logger.warn).toHaveBeenCalledWith("cognee-openclaw: failed to sync existing.md: 500 Internal error");
  });

  it("skips unchanged file", async () => {
    const files = [createFile("unchanged.md", "content", "hash-content")];
    const syncIndex: SyncIndex = { entries: { "unchanged.md": { hash: "hash-content", dataId: "id1" } } };

    const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

    expect(result.skipped).toBe(1);
    expect(mockAdd).not.toHaveBeenCalled();
  });

  it("deletes removed file with dataId", async () => {
    const syncIndex: SyncIndex = { entries: { "removed.md": { hash: "hash", dataId: "id1" } }, datasetId: "ds1" };
    mockDelete.mockResolvedValue({ datasetId: "ds1", dataId: "id1", deleted: true });

    const result = await syncFiles(client, [], [], syncIndex, cfg, logger);

    expect(result.deleted).toBe(1);
    expect(syncIndex.entries).toEqual({});
  });

  it("handles delete failure", async () => {
    const syncIndex: SyncIndex = { entries: { "removed.md": { hash: "hash", dataId: "id1" } }, datasetId: "ds1" };
    mockDelete.mockResolvedValue({ datasetId: "ds1", dataId: "id1", deleted: false });

    const result = await syncFiles(client, [], [], syncIndex, cfg, logger);

    expect(result.errors).toBe(1);
  });

  it("skips deletion without dataId or datasetId", async () => {
    const syncIndex: SyncIndex = { entries: { "removed.md": { hash: "hash" } }, datasetId: "ds1" };
    const result = await syncFiles(client, [], [], syncIndex, cfg, logger);
    expect(result.deleted).toBe(0);
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it("handles add, update, skip, delete in one sync", async () => {
    const files = [
      createFile("new.md", "new"),
      createFile("changed.md", "changed"),
      createFile("unchanged.md", "same", "hash-same"),
    ];
    const syncIndex: SyncIndex = {
      entries: { "changed.md": { hash: "old-hash", dataId: "id2" }, "unchanged.md": { hash: "hash-same", dataId: "id3" }, "removed.md": { hash: "hash", dataId: "id4" } },
      datasetId: "ds1",
    };
    mockAdd.mockResolvedValueOnce({ datasetId: "ds1", datasetName: "test", dataId: "id1" });
    mockUpdate.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id2" });
    mockDelete.mockResolvedValue({ datasetId: "ds1", dataId: "id4", deleted: true });

    const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

    expect(result).toEqual({ added: 1, updated: 1, skipped: 1, errors: 0, deleted: 1, datasetId: "ds1" });
  });

  it("triggers memify only after cognify completes (Fix #9)", async () => {
    _setPollInterval(1); // Use 1ms poll for tests
    cfg.autoMemify = true;
    const files = [createFile("new.md", "content")];
    const syncIndex: SyncIndex = { entries: {} };
    mockAdd.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id1" });
    mockDatasetStatus.mockResolvedValue("completed");

    await syncFiles(client, files, files, syncIndex, cfg, logger);

    expect(mockCognify).toHaveBeenCalled();
    // Memify should be called after polling shows cognify completed
    expect(mockMemify).toHaveBeenCalledWith({ datasetIds: ["ds1"] });
    _setPollInterval(5_000); // Reset
  });

  it("does not trigger memify when autoMemify is false", async () => {
    const files = [createFile("new.md", "content")];
    const syncIndex: SyncIndex = { entries: {} };
    mockAdd.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id1" });

    await syncFiles(client, files, files, syncIndex, cfg, logger);

    expect(mockMemify).not.toHaveBeenCalled();
  });

  it("does not delete unchanged files when called with partial changedFiles", async () => {
    const fullFiles = [createFile("unchanged.md", "old", "hash1"), createFile("changed.md", "new", "hash2")];
    const changedFiles = [fullFiles[1]];
    const syncIndex: SyncIndex = { entries: { "unchanged.md": { hash: "hash1", dataId: "id1" }, "changed.md": { hash: "oldhash", dataId: "id2" } } };
    mockUpdate.mockResolvedValue({});

    const result = await syncFiles(client, changedFiles, fullFiles, syncIndex, cfg, logger);

    expect(result.deleted).toBe(0);
    expect(mockDelete).not.toHaveBeenCalled();
  });

  it("uses overrideDatasetName for scoped sync", async () => {
    const files = [createFile("policy.md", "content")];
    const syncIndex: SyncIndex = { entries: {} };
    mockAdd.mockResolvedValue({ datasetId: "ds-company", datasetName: "acme-company", dataId: "id1" });

    await syncFiles(client, files, files, syncIndex, cfg, logger, "acme-company");

    expect(mockAdd).toHaveBeenCalledWith(expect.objectContaining({ datasetName: "acme-company" }));
  });

  it("calls onNewDataset callback when a new dataset is created", async () => {
    const files = [createFile("new.md", "content")];
    const syncIndex: SyncIndex = { entries: {} };
    mockAdd.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id1" });
    const onNewDataset = jest.fn().mockResolvedValue(undefined);

    await syncFiles(client, files, files, syncIndex, cfg, logger, undefined, onNewDataset);

    expect(onNewDataset).toHaveBeenCalledWith("ds1");
  });

  it("does not call onNewDataset callback on updates", async () => {
    const files = [createFile("existing.md", "new content")];
    const syncIndex: SyncIndex = { entries: { "existing.md": { hash: "old-hash", dataId: "id1" } }, datasetId: "ds1" };
    mockUpdate.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id1" });
    const onNewDataset = jest.fn().mockResolvedValue(undefined);

    await syncFiles(client, files, files, syncIndex, cfg, logger, undefined, onNewDataset);

    expect(onNewDataset).not.toHaveBeenCalled();
  });

  it("does not call onNewDataset callback when datasetId already known", async () => {
    const files = [createFile("new.md", "content")];
    const syncIndex: SyncIndex = { entries: {}, datasetId: "ds1" };
    mockAdd.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id1" });
    const onNewDataset = jest.fn().mockResolvedValue(undefined);

    await syncFiles(client, files, files, syncIndex, cfg, logger, undefined, onNewDataset);

    expect(onNewDataset).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// syncFilesScoped tests (Fix #6: properly typed ScopedSyncIndexes)
// ---------------------------------------------------------------------------

describe("syncFilesScoped", () => {
  let client: CogneeHttpClient;
  let cfg: Required<CogneePluginConfig>;
  let logger: { info?: jest.Mock; warn?: jest.Mock };

  beforeEach(() => {
    jest.clearAllMocks();
    mockFs.readFile.mockImplementation(async (path) => {
      if (path === STATE_PATH) return JSON.stringify({});
      if (path === SCOPED_SYNC_INDEX_PATH) return JSON.stringify({});
      throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
    });
    mockFs.writeFile.mockResolvedValue(undefined);
    mockFs.mkdir.mockResolvedValue(undefined);
    client = new CogneeHttpClient("http://test", "key");
    cfg = baseCfg({ companyDataset: "acme-company", userDatasetPrefix: "acme-user", agentDatasetPrefix: "acme-agent", userId: "alice", agentId: "coder" });
    logger = { info: jest.fn(), warn: jest.fn() };
  });

  it("routes files to correct scope datasets", async () => {
    const files = [
      createFile("memory/company/policy.md", "company policy"),
      createFile("memory/user/prefs.md", "user prefs"),
      createFile("memory/tools.md", "agent tools"),
    ];
    const scopedIndexes: ScopedSyncIndexes = {};
    mockAdd.mockImplementation(async (params: any) => ({
      datasetId: `ds-${params.datasetName}`,
      datasetName: params.datasetName,
      dataId: `id-${params.datasetName}`,
    }));

    const result = await syncFilesScoped(client, files, files, scopedIndexes, cfg, logger);

    expect(result.added).toBe(3);
    const addCalls = mockAdd.mock.calls.map((c: any[]) => c[0].datasetName);
    expect(addCalls).toContain("acme-company");
    expect(addCalls).toContain("acme-user-alice");
    expect(addCalls).toContain("acme-agent-coder");
  });

  it("handles mixed operations across scopes", async () => {
    const files = [
      createFile("memory/company/new.md", "new company doc"),
      createFile("memory/user/prefs.md", "updated prefs"),
    ];
    const scopedIndexes: ScopedSyncIndexes = {
      user: { entries: { "memory/user/prefs.md": { hash: "old-hash", dataId: "uid1" } }, datasetId: "ds-user" },
      agent: { entries: { "memory/removed.md": { hash: "hash", dataId: "aid1" } }, datasetId: "ds-agent" },
    };
    mockAdd.mockResolvedValue({ datasetId: "ds-company", datasetName: "acme-company", dataId: "cid1" });
    mockUpdate.mockResolvedValue({ datasetId: "ds-user", datasetName: "acme-user-alice", dataId: "uid1" });
    mockDelete.mockResolvedValue({ datasetId: "ds-agent", dataId: "aid1", deleted: true });

    const result = await syncFilesScoped(client, files, files, scopedIndexes, cfg, logger);

    expect(result.added).toBe(1);
    expect(result.updated).toBe(1);
    expect(result.deleted).toBe(1);
  });

  it("forwards onNewDataset callback with scope", async () => {
    const files = [
      createFile("memory/company/policy.md", "company policy"),
    ];
    const scopedIndexes: ScopedSyncIndexes = {};
    mockAdd.mockResolvedValue({ datasetId: "ds-company", datasetName: "acme-company", dataId: "cid1" });
    const onNewDataset = jest.fn().mockResolvedValue(undefined);

    await syncFilesScoped(client, files, files, scopedIndexes, cfg, logger, onNewDataset);

    expect(onNewDataset).toHaveBeenCalledWith("ds-company", "company");
  });

  it("skips scopes with no changes", async () => {
    const files = [createFile("memory/company/policy.md", "same", "hash-same")];
    const scopedIndexes: ScopedSyncIndexes = {
      company: { entries: { "memory/company/policy.md": { hash: "hash-same", dataId: "cid1" } }, datasetId: "ds-company" },
    };

    const result = await syncFilesScoped(client, files, files, scopedIndexes, cfg, logger);

    expect(result.skipped).toBe(1);
    expect(mockAdd).not.toHaveBeenCalled();
  });
});
