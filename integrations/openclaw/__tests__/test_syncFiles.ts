import { CogneeClient } from "../index";
import { resolveDatasetNameForAgent, syncFiles } from "../index";
import type { MemoryFile, SyncIndex, CogneePluginConfig } from "../index";
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

// Mock CogneeClient
jest.mock("../index", () => {
  const actual = jest.requireActual("../index");
  return {
    ...actual,
    CogneeClient: jest.fn(),
  };
});

const mockAdd = jest.fn();
const mockUpdate = jest.fn();
const mockDelete = jest.fn();
const mockCognify = jest.fn();

(CogneeClient as jest.MockedClass<typeof CogneeClient>).mockImplementation(() => ({
  add: mockAdd,
  update: mockUpdate,
  delete: mockDelete,
  cognify: mockCognify,
} as any));

describe("syncFiles", () => {
  let client: CogneeClient;
  let cfg: Required<CogneePluginConfig>;
  let logger: { info?: jest.Mock; warn?: jest.Mock };

  beforeEach(() => {
    jest.clearAllMocks();
    mockFs.readFile.mockImplementation(async (path) => {
      if (path === SYNC_INDEX_PATH) return JSON.stringify({ entries: {} });
      if (path === STATE_PATH) return JSON.stringify({});
      throw new Error(`Unexpected file read: ${path}`);
    });
    mockFs.writeFile.mockResolvedValue(undefined);
    mockFs.mkdir.mockResolvedValue(undefined);
    client = new CogneeClient("http://test", "key");
    cfg = {
      baseUrl: "http://test",
      apiKey: "key",
      username: "",
      password: "",
      datasetName: "test",
      datasetNames: {},
      searchPrompt: "",
      searchType: "GRAPH_COMPLETION",
      deleteMode: "soft",
      maxResults: 6,
      minScore: 0,
      maxTokens: 512,
      autoRecall: true,
      autoIndex: true,
      autoCognify: true,
      requestTimeoutMs: 30000,
      ingestionTimeoutMs: 300000,
    };
    logger = { info: jest.fn(), warn: jest.fn() };
  });

  const createFile = (path: string, content: string, hash?: string): MemoryFile => ({
    path,
    absPath: `/workspace/${path}`,
    content,
    hash: hash || `hash-${content}`,
  });

  describe("New file addition", () => {
    it("adds new file and updates syncIndex", async () => {
      const files = [createFile("new.md", "content")];
      const syncIndex: SyncIndex = { entries: {} };

      mockAdd.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id1" });

      const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

      expect(result).toEqual({ added: 1, updated: 0, skipped: 0, errors: 0, deleted: 0, datasetId: "ds1" });
      expect(mockAdd).toHaveBeenCalledWith({
        data: `# new.md\n\ncontent\n\n---\nMetadata: ${JSON.stringify({ path: "new.md", source: "memory" })}`,
        datasetName: "test",
        datasetId: undefined,
      });
      expect(syncIndex.entries["new.md"]).toEqual({ hash: "hash-content", dataId: "id1" });
      expect(mockCognify).toHaveBeenCalledWith({ datasetIds: ["ds1"] });
      expect(logger.info).toHaveBeenCalledWith("cognee-openclaw: added new.md");
    });
  });

  describe("File update", () => {
    it("updates changed file with dataId", async () => {
      const files = [createFile("existing.md", "new content")];
      const syncIndex: SyncIndex = {
        entries: { "existing.md": { hash: "old-hash", dataId: "id1" } },
        datasetId: "ds1",
      };

      mockUpdate.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id1" });

      const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

      expect(result).toEqual({ added: 0, updated: 1, skipped: 0, errors: 0, deleted: 0, datasetId: "ds1" });
      expect(mockUpdate).toHaveBeenCalledWith({
        dataId: "id1",
        datasetId: "ds1",
        data: `# existing.md\n\nnew content\n\n---\nMetadata: ${JSON.stringify({ path: "existing.md", source: "memory" })}`,
      });
      expect(syncIndex.entries["existing.md"]).toEqual({ hash: "hash-new content", dataId: "id1" });
      expect(mockCognify).not.toHaveBeenCalled();
    });

    it("falls back to add when update fails with 404", async () => {
      const files = [createFile("existing.md", "new content")];
      const syncIndex: SyncIndex = {
        entries: { "existing.md": { hash: "old-hash", dataId: "id1" } },
      };

      mockUpdate.mockRejectedValue(new Error("404 Not found"));
      mockAdd.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id2" });

      const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

      expect(result).toEqual({ added: 1, updated: 0, skipped: 0, errors: 0, deleted: 0, datasetId: "ds1" });
      expect(mockAdd).toHaveBeenCalled();
      expect(syncIndex.entries["existing.md"]).toEqual({ hash: "hash-new content", dataId: "id2" });
    });

    it("handles update failure without fallback", async () => {
      const files = [createFile("existing.md", "new content")];
      const syncIndex: SyncIndex = {
        entries: { "existing.md": { hash: "old-hash", dataId: "id1" } },
        datasetId: "ds1",
      };

      mockUpdate.mockRejectedValue(new Error("500 Internal error"));

      const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

      expect(result).toEqual({ added: 0, updated: 0, skipped: 0, errors: 1, deleted: 0, datasetId: "ds1" });
      expect(syncIndex.entries["existing.md"]).toEqual({ hash: "old-hash", dataId: "id1" });
      expect(logger.warn).toHaveBeenCalledWith("cognee-openclaw: failed to sync existing.md: 500 Internal error");
    });
  });

  describe("Unchanged file", () => {
    it("skips unchanged file", async () => {
      const files = [createFile("unchanged.md", "content", "hash-content")];
      const syncIndex: SyncIndex = {
        entries: { "unchanged.md": { hash: "hash-content", dataId: "id1" } },
      };

      const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

      expect(result).toEqual({ added: 0, updated: 0, skipped: 1, errors: 0, deleted: 0 });
      expect(mockAdd).not.toHaveBeenCalled();
      expect(mockUpdate).not.toHaveBeenCalled();
    });
  });

  describe("File deletion", () => {
    it("deletes removed file with dataId", async () => {
      const files: MemoryFile[] = [];
      const syncIndex: SyncIndex = {
        entries: { "removed.md": { hash: "hash", dataId: "id1" } },
        datasetId: "ds1",
      };

      mockDelete.mockResolvedValue({ datasetId: "ds1", dataId: "id1", deleted: true });

      const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

      expect(result).toEqual({ added: 0, updated: 0, skipped: 0, errors: 0, deleted: 1, datasetId: "ds1" });
      expect(mockDelete).toHaveBeenCalledWith({ dataId: "id1", datasetId: "ds1", mode: "soft" });
      expect(syncIndex.entries).toEqual({});
      expect(mockCognify).not.toHaveBeenCalled();
      expect(logger.info).toHaveBeenCalledWith("cognee-openclaw: deleted removed.md");
    });

    it("handles delete failure", async () => {
      const files: MemoryFile[] = [];
      const syncIndex: SyncIndex = {
        entries: { "removed.md": { hash: "hash", dataId: "id1" } },
        datasetId: "ds1",
      };

      mockDelete.mockResolvedValue({ datasetId: "ds1", dataId: "id1", deleted: false });

      const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

      expect(result).toEqual({ added: 0, updated: 0, skipped: 0, errors: 1, deleted: 0, datasetId: "ds1" });
      expect(syncIndex.entries["removed.md"]).toEqual({ hash: "hash", dataId: "id1" });
      expect(logger.warn).toHaveBeenCalledWith("cognee-openclaw: failed to delete removed.md");
    });

    it("skips deletion without dataId", async () => {
      const files: MemoryFile[] = [];
      const syncIndex: SyncIndex = {
        entries: { "removed.md": { hash: "hash" } }, // no dataId
        datasetId: "ds1",
      };

      const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

      expect(result).toEqual({ added: 0, updated: 0, skipped: 0, errors: 0, deleted: 0, datasetId: "ds1" });
      expect(mockDelete).not.toHaveBeenCalled();
      expect(syncIndex.entries["removed.md"]).toEqual({ hash: "hash" });
    });

    it("skips deletion without datasetId", async () => {
      const files: MemoryFile[] = [];
      const syncIndex: SyncIndex = {
        entries: { "removed.md": { hash: "hash", dataId: "id1" } },
        // no datasetId
      };

      const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

      expect(result).toEqual({ added: 0, updated: 0, skipped: 0, errors: 0, deleted: 0 });
      expect(mockDelete).not.toHaveBeenCalled();
    });
  });

  describe("Add failure", () => {
    it("handles add failure", async () => {
      const files = [createFile("new.md", "content")];
      const syncIndex: SyncIndex = { entries: {} };

      mockAdd.mockRejectedValue(new Error("Add failed"));

      const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

      expect(result).toEqual({ added: 0, updated: 0, skipped: 0, errors: 1, deleted: 0 });
      expect(syncIndex.entries).toEqual({});
      expect(logger.warn).toHaveBeenCalledWith("cognee-openclaw: failed to sync new.md: Add failed");
    });
  });

  describe("Mixed operations", () => {
    it("handles add, update, skip, delete in one sync", async () => {
      const files = [
        createFile("new.md", "new"),
        createFile("changed.md", "changed"),
        createFile("unchanged.md", "same", "hash-same"),
      ];
      const syncIndex: SyncIndex = {
        entries: {
          "changed.md": { hash: "old-hash", dataId: "id2" },
          "unchanged.md": { hash: "hash-same", dataId: "id3" },
          "removed.md": { hash: "hash", dataId: "id4" },
        },
        datasetId: "ds1",
      };

      mockAdd.mockResolvedValueOnce({ datasetId: "ds1", datasetName: "test", dataId: "id1" });
      mockUpdate.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id2" });
      mockDelete.mockResolvedValue({ datasetId: "ds1", dataId: "id4", deleted: true });

      const result = await syncFiles(client, files, files, syncIndex, cfg, logger);

      expect(result).toEqual({ added: 1, updated: 1, skipped: 1, errors: 0, deleted: 1, datasetId: "ds1" });
      expect(mockAdd).toHaveBeenCalledTimes(1);
      expect(mockUpdate).toHaveBeenCalledTimes(1);
      expect(mockDelete).toHaveBeenCalledTimes(1);
      expect(mockCognify).toHaveBeenCalledWith({ datasetIds: ["ds1"] });
    });
  });

  describe("Cognify behavior", () => {
    it("triggers cognify on adds", async () => {
      const files = [createFile("new.md", "content")];
      const syncIndex: SyncIndex = { entries: {} };

      mockAdd.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id1" });

      await syncFiles(client, files, files, syncIndex, cfg, logger);

      expect(mockCognify).toHaveBeenCalledWith({ datasetIds: ["ds1"] });
    });

    it("does not trigger cognify on pure deletions (cognify only runs after adds)", async () => {
      const files: MemoryFile[] = [];
      const syncIndex: SyncIndex = {
        entries: { "removed.md": { hash: "hash", dataId: "id1" } },
        datasetId: "ds1",
      };

      mockDelete.mockResolvedValue({ datasetId: "ds1", dataId: "id1", deleted: true });

      await syncFiles(client, files, files, syncIndex, cfg, logger);

      expect(mockCognify).not.toHaveBeenCalled();
    });

    it("does not trigger cognify when autoCognify is false", async () => {
      cfg.autoCognify = false;
      const files = [createFile("new.md", "content")];
      const syncIndex: SyncIndex = { entries: {} };

      mockAdd.mockResolvedValue({ datasetId: "ds1", datasetName: "test", dataId: "id1" });

      await syncFiles(client, files, files, syncIndex, cfg, logger);

      expect(mockCognify).not.toHaveBeenCalled();
    });

    it("does not delete unchanged files when called with partial changedFiles", async () => {
      const fullFiles = [
        createFile("unchanged.md", "old content", "hash1"),
        createFile("changed.md", "new content", "hash2")
      ];
      const changedFiles = [fullFiles[1]]; // only changed
      const syncIndex: SyncIndex = {
        entries: {
          "unchanged.md": { hash: "hash1", dataId: "id_unchanged" },
          "changed.md": { hash: "oldhash", dataId: "id_changed" }
        }
      };

      mockUpdate.mockResolvedValue({});

      const result = await syncFiles(client, changedFiles, fullFiles, syncIndex, cfg, logger);

      expect(result.deleted).toBe(0);
      expect(mockDelete).not.toHaveBeenCalled();
    });
  });
});

describe("resolveDatasetNameForAgent", () => {
  const cfg: Required<CogneePluginConfig> = {
    baseUrl: "http://test",
    apiKey: "key",
    username: "",
    password: "",
    datasetName: "shared-default",
    datasetNames: {
      lawyer: "legal-memory",
      lexi: "media-memory",
      asst: "shared-default",
    },
    searchPrompt: "",
    searchType: "GRAPH_COMPLETION",
    deleteMode: "soft",
    maxResults: 6,
    minScore: 0,
    maxTokens: 512,
    autoRecall: true,
    autoIndex: true,
    autoCognify: true,
    requestTimeoutMs: 30000,
    ingestionTimeoutMs: 300000,
  };

  it("falls back to the default dataset for agents without an override", () => {
    expect(resolveDatasetNameForAgent(cfg, "researcher")).toBe("shared-default");
  });

  it("uses datasetName when an agent is omitted from datasetNames in a mixed config", () => {
    expect(resolveDatasetNameForAgent(cfg, "elena")).toBe("shared-default");
    expect(resolveDatasetNameForAgent(cfg, undefined)).toBe("shared-default");
  });

  it("returns exclusive datasets for agents with dedicated overrides", () => {
    expect(resolveDatasetNameForAgent(cfg, "lawyer")).toBe("legal-memory");
    expect(resolveDatasetNameForAgent(cfg, "lexi")).toBe("media-memory");
  });

  it("allows explicit sharing by pointing multiple agents at the same dataset", () => {
    expect(resolveDatasetNameForAgent(cfg, "asst")).toBe("shared-default");
    expect(resolveDatasetNameForAgent(cfg, "elena")).toBe("shared-default");
  });
});