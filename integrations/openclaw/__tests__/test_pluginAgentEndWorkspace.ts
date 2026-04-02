import plugin from "../src/plugin";

const mockCollectMemoryFiles = jest.fn();
const mockSyncFiles = jest.fn();
const mockSyncFilesScoped = jest.fn();
const mockLoadDatasetState = jest.fn();
const mockLoadScopedSyncIndexes = jest.fn();
const mockLoadSyncIndex = jest.fn();
const mockMigrateLegacyIndex = jest.fn();

const mockHealth = jest.fn();
const mockFetchJson = jest.fn();

jest.mock("../src/files", () => ({
  collectMemoryFiles: (...args: unknown[]) => mockCollectMemoryFiles(...args),
}));

jest.mock("../src/sync", () => ({
  syncFiles: (...args: unknown[]) => mockSyncFiles(...args),
  syncFilesScoped: (...args: unknown[]) => mockSyncFilesScoped(...args),
}));

jest.mock("../src/persistence", () => ({
  loadDatasetState: (...args: unknown[]) => mockLoadDatasetState(...args),
  loadScopedSyncIndexes: (...args: unknown[]) => mockLoadScopedSyncIndexes(...args),
  loadSyncIndex: (...args: unknown[]) => mockLoadSyncIndex(...args),
  migrateLegacyIndex: (...args: unknown[]) => mockMigrateLegacyIndex(...args),
  SYNC_INDEX_PATH: "/tmp/sync-index.json",
  SCOPED_SYNC_INDEX_PATH: "/tmp/scoped-sync-indexes.json",
}));

jest.mock("../src/client", () => ({
  CogneeHttpClient: jest.fn().mockImplementation(() => ({
    health: mockHealth,
    fetchJson: mockFetchJson,
  })),
}));

type AgentEndHandler = (event: { success: boolean }, ctx: Record<string, unknown>) => Promise<void>;
type ServiceRegistration = {
  id: string;
  start: (ctx: { workspaceDir?: string; logger: { info?: jest.Mock; warn?: jest.Mock } }) => Promise<void>;
};

function createApi(params?: {
  runtimeWorkspaceResolver?: (config: unknown, agentId: string) => string | undefined;
}) {
  const handlers = new Map<string, AgentEndHandler>();
  const services: ServiceRegistration[] = [];
  const logger = {
    info: jest.fn(),
    warn: jest.fn(),
    debug: jest.fn(),
  };

  const api = {
    id: "cognee-openclaw",
    name: "Memory (Cognee)",
    source: "test",
    config: {
      agents: {
        defaults: {
          workspace: "/default/workspace",
        },
      },
    },
    pluginConfig: {
      autoIndex: true,
      autoRecall: false,
      enableSessions: false,
    },
    runtime: {
      agent: {
        resolveAgentWorkspaceDir: params?.runtimeWorkspaceResolver,
      },
    },
    logger,
    registerCli: jest.fn(),
    registerService: jest.fn((registration: ServiceRegistration) => {
      services.push(registration);
    }),
    on: jest.fn((eventName: string, handler: AgentEndHandler) => {
      handlers.set(eventName, handler);
    }),
  };

  plugin.register(api as never);

  const agentEnd = handlers.get("agent_end");
  if (!agentEnd) {
    throw new Error("agent_end handler was not registered");
  }

  return {
    api,
    logger,
    agentEnd,
    service: services[0],
  };
}

describe("cognee-openclaw agent_end workspace resolution", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockLoadDatasetState.mockResolvedValue({});
    mockLoadScopedSyncIndexes.mockResolvedValue({});
    mockLoadSyncIndex.mockResolvedValue({ entries: {} });
    mockMigrateLegacyIndex.mockResolvedValue(undefined);
    mockCollectMemoryFiles.mockResolvedValue([
      {
        path: "MEMORY.md",
        absPath: "/resolved/workspace/MEMORY.md",
        content: "hello",
        hash: "hash-1",
      },
    ]);
    mockSyncFiles.mockResolvedValue({
      added: 0,
      updated: 0,
      skipped: 0,
      errors: 0,
      deleted: 0,
      datasetId: undefined,
    });
    mockSyncFilesScoped.mockResolvedValue({
      added: 0,
      updated: 0,
      skipped: 0,
      errors: 0,
      deleted: 0,
    });
    mockHealth.mockResolvedValue({ status: "ok" });
    mockFetchJson.mockResolvedValue({});
  });

  it("uses hook context workspaceDir when present", async () => {
    const { agentEnd, logger } = createApi({
      runtimeWorkspaceResolver: () => "/agent/runtime/workspace",
    });

    await agentEnd(
      { success: true },
      { workspaceDir: "/hook/workspace", agentId: "coder" },
    );

    expect(mockCollectMemoryFiles).toHaveBeenCalledWith("/hook/workspace");
    expect(logger.info).toHaveBeenCalledWith(
      "cognee-openclaw: agent_end workspace resolved from hook context: /hook/workspace",
    );
  });

  it("falls back to runtime agent workspace when hook context workspace is missing", async () => {
    const { agentEnd, logger } = createApi({
      runtimeWorkspaceResolver: (_config, agentId) =>
        agentId === "coder" ? "/agents/coder-workspace" : undefined,
    });

    await agentEnd(
      { success: true },
      { agentId: "coder" },
    );

    expect(mockCollectMemoryFiles).toHaveBeenCalledWith("/agents/coder-workspace");
    expect(logger.info).toHaveBeenCalledWith(
      "cognee-openclaw: agent_end workspace resolved from agent runtime: /agents/coder-workspace",
    );
  });

  it("falls back to cached service workspace when hook context and runtime workspace are missing", async () => {
    const { agentEnd, logger, service } = createApi();

    if (!service) {
      throw new Error("service was not registered");
    }

    await service.start({
      workspaceDir: "/service/workspace",
      logger,
    });
    mockCollectMemoryFiles.mockClear();
    logger.info.mockClear();

    await agentEnd(
      { success: true },
      { agentId: "coder" },
    );

    expect(mockCollectMemoryFiles).toHaveBeenCalledWith("/service/workspace");
    expect(logger.info).toHaveBeenCalledWith(
      "cognee-openclaw: agent_end workspace resolved from service cache: /service/workspace",
    );
  });

  it("falls back to process.cwd() when no other workspace source exists", async () => {
    const { agentEnd, logger } = createApi();

    await agentEnd(
      { success: true },
      {},
    );

    expect(mockCollectMemoryFiles).toHaveBeenCalledWith(process.cwd());
    expect(logger.info).toHaveBeenCalledWith(
      `cognee-openclaw: agent_end workspace resolved from process.cwd(): ${process.cwd()}`,
    );
  });
});
