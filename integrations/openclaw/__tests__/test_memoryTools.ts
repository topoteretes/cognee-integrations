import plugin from "../src/plugin";
import { CogneeHttpClient } from "../src/client";

jest.mock("../src/client");
jest.mock("../src/server", () => ({
  bootServerIfNeeded: jest.fn(async () => {}),
  waitForServerHealth: jest.fn(async () => {}),
  isLocalUrl: jest.fn(() => true),
  resolveOrMintApiKey: jest.fn(async () => "test-api-key"),
  spawnExitWatcher: jest.fn(async () => {}),
  exitWatcherPidfilePath: jest.fn((name: string) => `/tmp/exit-watchers/${name}.pid`),
}));

jest.mock("../src/persistence", () => ({
  loadDatasetState: jest.fn(async () => ({ testds: "dataset-1" })),
  saveDatasetState: jest.fn(async () => {}),
  loadSyncIndex: jest.fn(async () => ({ entries: {}, datasetId: "dataset-1", datasetName: "testds" })),
  saveSyncIndex: jest.fn(async () => {}),
  loadScopedSyncIndexes: jest.fn(async () => ({})),
  saveScopedSyncIndexes: jest.fn(async () => {}),
  loadAgentSyncIndexes: jest.fn(async () => ({})),
  saveAgentSyncIndexes: jest.fn(async () => {}),
  migrateLegacyIndex: jest.fn(async () => null),
  migrateAgentScopeToPerAgent: jest.fn(async () => null),
  SYNC_INDEX_PATH: "/tmp/sync-index.json",
}));

const mockRecall = jest.fn(async () => ([{
  id: "memory-1",
  text: "Neeraj prefers evidence over reassurance.",
  score: 0.94,
  metadata: {
    source: "MEMORY.md",
    created_at: "2026-07-31T20:00:00Z",
  },
}]));

type Tool = {
  name: string;
  execute: (id: string, params: Record<string, unknown>) => Promise<{ details?: unknown; content: unknown[] }>;
};

function createApi() {
  const factories = new Map<string, (ctx: Record<string, unknown>) => Tool>();
  const api = {
    id: "cognee-openclaw",
    name: "Memory (Cognee)",
    source: "test",
    config: {},
    pluginConfig: {
      autoIndex: false,
      autoRecall: false,
      enableSessions: false,
      datasetName: "testds",
      minScore: 0,
    },
    runtime: {},
    logger: { info: jest.fn(), warn: jest.fn(), debug: jest.fn() },
    registerMemoryFlushPlan: jest.fn(),
    registerCli: jest.fn(),
    registerService: jest.fn(),
    registerTool: jest.fn((factory: (ctx: Record<string, unknown>) => Tool, opts: { names: string[] }) => {
      for (const name of opts.names) factories.set(name, factory);
    }),
    on: jest.fn(),
  };
  plugin.register(api as never);

  const tool = (name: string): Tool => {
    const factory = factories.get(name);
    if (!factory) throw new Error(`tool not registered: ${name}`);
    return factory({ agentId: "main", sessionId: "s1" });
  };
  return { api, tool };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockRecall.mockResolvedValue([{
    id: "memory-1",
    text: "Neeraj prefers evidence over reassurance.",
    score: 0.94,
    metadata: { source: "MEMORY.md", created_at: "2026-07-31T20:00:00Z" },
  }]);
  (CogneeHttpClient as unknown as jest.Mock).mockImplementation(() => ({
    recall: mockRecall,
    listDatasets: jest.fn(async () => [{ id: "dataset-1", name: "testds" }]),
  }));
});

describe("OpenClaw memory tool contract", () => {
  it("registers memory_search and memory_get independently of autoRecall", () => {
    const { api } = createApi();

    expect(api.registerTool).toHaveBeenCalledTimes(2);
    expect(api.registerTool.mock.calls.map((call) => call[1].names[0])).toEqual([
      "memory_search",
      "memory_get",
    ]);
  });

  it("returns search references with score, scope, source, and time", async () => {
    const { tool } = createApi();
    const result = await tool("memory_search").execute("call-1", { query: "What does Neeraj prefer?" });

    expect(result.details).toEqual(expect.objectContaining({
      results: [expect.objectContaining({
        path: expect.stringMatching(/^cognee:[a-f0-9]{24}$/),
        text: "Neeraj prefers evidence over reassurance.",
        score: 0.94,
        scope: "agent",
        source: "MEMORY.md",
        time: "2026-07-31T20:00:00Z",
      })],
      provider: "cognee",
    }));
    expect(JSON.parse((result.content[0] as { text: string }).text)).toEqual(result.details);
  });

  it("returns structured unavailable output when recall fails", async () => {
    mockRecall.mockRejectedValueOnce(new Error("recall timed out"));
    const { tool } = createApi();

    const result = await tool("memory_search").execute("call-1", { query: "prior decision" });

    expect(result.details).toEqual(expect.objectContaining({
      results: [],
      unavailable: true,
      disabled: true,
      error: "recall timed out",
    }));
  });

  it("applies maxResults and minScore overrides", async () => {
    mockRecall.mockResolvedValueOnce([
      { id: "a", text: "best", score: 0.9, metadata: {} as never },
      { id: "b", text: "weak", score: 0.5, metadata: {} as never },
      { id: "c", text: "second", score: 0.8, metadata: {} as never },
    ]);
    const { tool } = createApi();

    const result = await tool("memory_search").execute("call-1", {
      query: "prior decision",
      maxResults: 1,
      minScore: 0.7,
    });

    const payload = result.details as { results: Array<{ text: string }> };
    expect(payload.results).toHaveLength(1);
    expect(payload.results[0]!.text).toBe("best");
    expect(mockRecall).toHaveBeenCalledWith(expect.objectContaining({ topK: 1 }));
  });

  it("resolves a search reference through memory_get with provenance", async () => {
    const { tool } = createApi();
    const search = tool("memory_search");
    const get = tool("memory_get");
    const searchResult = await search.execute("call-1", { query: "What does Neeraj prefer?" });
    const reference = (searchResult.details as { results: Array<{ path: string }> }).results[0]!.path;

    const result = await get.execute("call-2", { path: reference });

    expect(result.details).toEqual(expect.objectContaining({
      path: reference,
      text: "Neeraj prefers evidence over reassurance.",
      scope: "agent",
      source: "MEMORY.md",
      time: "2026-07-31T20:00:00Z",
    }));
  });

  it("returns a structured not-found result for stale references", async () => {
    const { tool } = createApi();
    const result = await tool("memory_get").execute("call-1", { path: "cognee:agent:missing" });

    expect(result.details).toEqual({
      path: "cognee:agent:missing",
      text: "",
      status: "not_found",
      found: false,
      notFound: true,
      error: "Cognee memory reference not found or expired",
    });
  });
});
