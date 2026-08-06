import plugin from "../src/plugin";
import { CogneeHttpClient } from "../src/client";
import { loadDatasetState, saveDatasetState } from "../src/persistence";

jest.mock("../src/client");
jest.mock("../src/server", () => ({
  bootServerIfNeeded: jest.fn(async () => {}),
  waitForServerHealth: jest.fn(async () => {}),
  isLocalUrl: jest.fn(() => true),
  resolveOrMintApiKey: jest.fn(async () => "test-api-key"),
  spawnExitWatcher: jest.fn(async () => {}),
  exitWatcherPidfilePath: jest.fn((name: string) => `/tmp/exit-watchers/${name}.pid`),
}));

// Never touch the real shared ~/.cognee-plugin/recall-breaker.json in tests.
const mockBreaker = {
  openForSeconds: jest.fn(async () => 0),
  recordFailure: jest.fn(async (_msg: string) => {}),
  recordSuccess: jest.fn(async () => {}),
};
jest.mock("../src/breaker", () => ({
  RecallBreaker: jest.fn(() => mockBreaker),
  isBreakerError: (e: unknown) => {
    const status = /\((\d{3})\)/.exec(String(e))?.[1];
    return status ? status.startsWith("5") : true;
  },
}));

// In-memory dataset state so healing tests never touch ~/.openclaw on disk.
let datasetState: Record<string, string> = {};
jest.mock("../src/persistence", () => ({
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

const staleError = new Error(
  'Cognee request failed (403): {"error":"Recall prerequisites not met"}',
);

const mockRecall = jest.fn(async (_params: unknown): Promise<unknown[]> => []);
const mockListDatasets = jest.fn(async (): Promise<{ id: string; name: string }[]> => []);

type HookHandler = (event: unknown, ctx: unknown) => Promise<unknown> | unknown;

function createApi() {
  const handlers = new Map<string, HookHandler[]>();
  const api = {
    id: "cognee-openclaw",
    name: "Memory (Cognee)",
    source: "test",
    config: {},
    pluginConfig: {
      autoIndex: false,
      autoRecall: true,
      enableSessions: false,
      captureSession: false,
      datasetName: "testds",
      minScore: 0,
    },
    runtime: {},
    logger: { info: jest.fn(), warn: jest.fn(), debug: jest.fn() },
    registerMemoryFlushPlan: jest.fn(),
    registerCli: jest.fn(),
    registerService: jest.fn(),
    on: jest.fn((name: string, fn: HookHandler) => {
      const list = handlers.get(name) ?? [];
      list.push(fn);
      handlers.set(name, list);
    }),
  };
  plugin.register(api as never);

  const emit = async (name: string, event: unknown, ctx: unknown) => {
    const results = [];
    for (const fn of handlers.get(name) ?? []) {
      results.push(await fn(event, ctx));
    }
    return results;
  };
  return { api, emit };
}

beforeEach(() => {
  jest.clearAllMocks();
  datasetState = { testds: "stale-id" };
  mockBreaker.openForSeconds.mockImplementation(async () => 0);
  mockRecall.mockImplementation(async () => []);
  mockListDatasets.mockImplementation(async () => []);
  (CogneeHttpClient as unknown as jest.Mock).mockImplementation(() => ({
    recall: mockRecall,
    listDatasets: mockListDatasets,
    health: jest.fn(async () => ({ status: "ok" })),
    setApiKey: jest.fn(),
  }));
});

describe("recall self-healing on stale dataset ids", () => {
  it("invalidates the cache, re-resolves by name, and retries once", async () => {
    mockRecall
      .mockRejectedValueOnce(staleError)
      .mockResolvedValueOnce([{ id: "r1", text: "hello", score: 0.9 }]);
    mockListDatasets.mockResolvedValue([{ id: "fresh-id", name: "testds" }]);

    const { emit } = createApi();
    const results = await emit("before_prompt_build", { prompt: "what did we discuss" }, { agentId: "will" });
    const result = results.find((r) => r !== undefined);

    expect(mockRecall).toHaveBeenCalledTimes(2);
    const firstIds = (mockRecall.mock.calls[0]![0] as { datasetIds: string[] }).datasetIds;
    const secondIds = (mockRecall.mock.calls[1]![0] as { datasetIds: string[] }).datasetIds;
    expect(firstIds).toEqual(["stale-id"]);
    expect(secondIds).toEqual(["fresh-id"]);

    // Cache rewritten with the fresh id.
    expect(datasetState.testds).toBe("fresh-id");
    expect(saveDatasetState).toHaveBeenCalled();

    // The healed recall still injects memories.
    expect(result).toBeDefined();
    expect(JSON.stringify(result)).toContain("hello");
  });

  it("gives up quietly when the dataset no longer exists on the server", async () => {
    mockRecall.mockRejectedValueOnce(staleError);
    mockListDatasets.mockResolvedValue([]); // dataset genuinely gone

    const { api, emit } = createApi();
    const results = await emit("before_prompt_build", { prompt: "what did we discuss" }, { agentId: "will" });

    expect(mockRecall).toHaveBeenCalledTimes(1);
    expect(results.find((r) => r !== undefined)).toBeUndefined();
    // Stale entry removed from the cache even though nothing replaced it.
    expect(datasetState.testds).toBeUndefined();
    expect(api.logger.warn).toHaveBeenCalledWith(expect.stringContaining("not found on server"));
  });

  it("does not retry when re-resolution returns the same id (real permission problem)", async () => {
    mockRecall.mockRejectedValue(staleError);
    mockListDatasets.mockResolvedValue([{ id: "stale-id", name: "testds" }]);

    const { emit } = createApi();
    const results = await emit("before_prompt_build", { prompt: "what did we discuss" }, { agentId: "will" });

    // Only the original attempt — same id would just fail again.
    expect(mockRecall).toHaveBeenCalledTimes(1);
    expect(results.find((r) => r !== undefined)).toBeUndefined();
  });

  it("does not heal on unrelated errors", async () => {
    mockRecall.mockRejectedValue(new Error("Cognee request failed (500): boom"));

    const { emit } = createApi();
    await emit("before_prompt_build", { prompt: "what did we discuss" }, { agentId: "will" });

    expect(mockRecall).toHaveBeenCalledTimes(1);
    expect(mockListDatasets).not.toHaveBeenCalled();
    expect(datasetState.testds).toBe("stale-id");
  });

  it("loadDatasetState mock sanity", async () => {
    await expect(loadDatasetState()).resolves.toEqual({ testds: "stale-id" });
  });
});

describe("recall budget + circuit breaker", () => {
  it("skips recall entirely while the breaker is open", async () => {
    mockBreaker.openForSeconds.mockImplementation(async () => 42);

    const { api, emit } = createApi();
    await emit("before_prompt_build", { prompt: "what did we discuss" }, { agentId: "will" });

    expect(mockRecall).not.toHaveBeenCalled();
    expect(api.logger.info).toHaveBeenCalledWith(expect.stringContaining("breaker open"));
  });

  it("records a breaker failure on 5xx but not on 4xx", async () => {
    mockRecall.mockRejectedValue(new Error("Cognee request failed (503): busy"));
    const { emit } = createApi();
    await emit("before_prompt_build", { prompt: "what did we discuss" }, { agentId: "will" });
    expect(mockBreaker.recordFailure).toHaveBeenCalledTimes(1);

    jest.clearAllMocks();
    mockBreaker.openForSeconds.mockImplementation(async () => 0);
    mockRecall.mockRejectedValue(staleError); // 403 — heal path, must not trip
    mockListDatasets.mockResolvedValue([]);
    const second = createApi();
    await second.emit("before_prompt_build", { prompt: "what did we discuss" }, { agentId: "will" });
    expect(mockBreaker.recordFailure).not.toHaveBeenCalled();
  });

  it("records success after a working recall", async () => {
    mockRecall.mockResolvedValue([{ id: "r1", text: "hello", score: 0.9 }]);
    datasetState = { testds: "good-id" };

    const { emit } = createApi();
    await emit("before_prompt_build", { prompt: "what did we discuss" }, { agentId: "will" });

    expect(mockBreaker.recordSuccess).toHaveBeenCalled();
  });

  it("drops the turn's memories when the recall budget is exceeded", async () => {
    jest.useFakeTimers();
    try {
      mockRecall.mockImplementation(() => new Promise(() => {})); // never resolves
      datasetState = { testds: "good-id" };

      const { api, emit } = createApi();
      const pending = emit("before_prompt_build", { prompt: "what did we discuss" }, { agentId: "will" });
      await jest.advanceTimersByTimeAsync(4_500); // past the 4s default budget
      const results = await pending;

      expect(results.find((r) => r !== undefined)).toBeUndefined();
      expect(api.logger.warn).toHaveBeenCalledWith(expect.stringContaining("recall budget"));
    } finally {
      jest.useRealTimers();
    }
  });

  it("passes the short recall timeout to the client", async () => {
    mockRecall.mockResolvedValue([]);
    datasetState = { testds: "good-id" };

    const { emit } = createApi();
    await emit("before_prompt_build", { prompt: "what did we discuss" }, { agentId: "will" });

    expect(mockRecall).toHaveBeenCalledWith(expect.objectContaining({ timeoutMs: 2_500 }));
  });
});
