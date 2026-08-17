import plugin from "../src/plugin";

type CliAction = (opts: Record<string, unknown>) => Promise<void> | void;

// Minimal commander stand-in: records each subcommand's action by name.
function createFakeProgram(actions: Map<string, CliAction>) {
  function makeCommand(name: string) {
    const cmd = {
      command: (sub: string) => makeCommand(sub),
      description: () => cmd,
      option: () => cmd,
      action: (fn: CliAction) => {
        actions.set(name, fn);
        return cmd;
      },
    };
    return cmd;
  }
  return { command: (name: string) => makeCommand(name) };
}

function createApi(loadedConfig: Record<string, unknown>) {
  const mutateConfigFile = jest.fn(
    async (params: { mutate: (draft: Record<string, unknown>, ctx: unknown) => unknown }) => {
      await params.mutate(loadedConfig, { snapshot: {}, previousHash: null });
      return { result: undefined };
    },
  );
  const actions = new Map<string, CliAction>();

  const api = {
    id: "cognee-openclaw",
    name: "Memory (Cognee)",
    source: "test",
    config: {},
    pluginConfig: {
      autoIndex: false,
      autoRecall: false,
      enableSessions: false,
    },
    runtime: {
      config: {
        current: jest.fn(() => loadedConfig),
        mutateConfigFile,
      },
    },
    logger: {
      info: jest.fn(),
      warn: jest.fn(),
      debug: jest.fn(),
    },
    registerCli: jest.fn((cb: (ctx: unknown) => void) => {
      cb({ program: createFakeProgram(actions), workspaceDir: "/tmp/test-ws", logger: api.logger });
    }),
    registerService: jest.fn(),
    registerTool: jest.fn(),
    on: jest.fn(),
  };

  plugin.register(api as never);
  return { actions, mutateConfigFile, loadedConfig };
}

describe("openclaw cognee setup", () => {
  let exitSpy: jest.SpyInstance;
  let logSpy: jest.SpyInstance;

  beforeEach(() => {
    jest.clearAllMocks();
    exitSpy = jest.spyOn(process, "exit").mockImplementation((() => undefined) as never);
    logSpy = jest.spyOn(console, "log").mockImplementation(() => undefined);
  });

  afterEach(() => {
    exitSpy.mockRestore();
    logSpy.mockRestore();
  });

  it("writes both hook permissions on the cognee-openclaw entry", async () => {
    const { actions, mutateConfigFile, loadedConfig } = createApi({});

    const setup = actions.get("setup");
    expect(setup).toBeDefined();
    await setup!({});

    expect(mutateConfigFile).toHaveBeenCalledTimes(1);
    expect(mutateConfigFile.mock.calls[0][0]).toMatchObject({ afterWrite: { mode: "auto" } });
    const written = loadedConfig as never as {
      plugins: { slots: Record<string, string>; entries: Record<string, unknown> };
    };
    expect(written.plugins.slots.memory).toBe("cognee-openclaw");
    expect(written.plugins.entries["cognee-openclaw"]).toEqual({
      enabled: true,
      hooks: {
        allowPromptInjection: true,
        allowConversationAccess: true,
      },
    });
    expect(written.plugins.entries["memory-core"]).toEqual({ enabled: false });
    expect(written.plugins.entries["memory-lancedb"]).toEqual({ enabled: false });
  });

  it("preserves existing hooks keys and keeps memory-core enabled in hybrid mode", async () => {
    const { actions, loadedConfig } = createApi({
      plugins: {
        entries: {
          "cognee-openclaw": {
            enabled: false,
            hooks: { timeoutMs: 9000, allowPromptInjection: false },
          },
        },
      },
    });

    await actions.get("setup")!({ hybrid: true });

    const written = loadedConfig as never as {
      plugins: { entries: Record<string, { enabled: boolean }> };
    };
    expect(written.plugins.entries["cognee-openclaw"]).toEqual({
      enabled: true,
      hooks: {
        timeoutMs: 9000,
        allowPromptInjection: true,
        allowConversationAccess: true,
      },
    });
    expect(written.plugins.entries["memory-core"].enabled).toBe(true);
    expect(written.plugins.entries["memory-lancedb"]).toBeUndefined();
  });
});
