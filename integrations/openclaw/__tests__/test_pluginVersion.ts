import plugin from "../src/plugin";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const getDir = () => {
  if (typeof __dirname !== "undefined") {
    return __dirname;
  }
  const metaUrl = new Function("return import.meta.url")();
  return dirname(fileURLToPath(metaUrl));
};
const dir = getDir();
const pkg = JSON.parse(readFileSync(resolve(dir, "../package.json"), "utf8"));
const expectedVersion = pkg.version;

describe("cognee-openclaw status command version display", () => {
  let spyLog: jest.SpyInstance;
  let spyExit: jest.SpyInstance;
  let mockFetch: jest.SpyInstance;

  beforeEach(() => {
    spyLog = jest.spyOn(console, "log").mockImplementation(() => {});
    spyExit = jest.spyOn(process, "exit").mockImplementation((() => {}) as any);
    mockFetch = jest.spyOn(global, "fetch");
  });

  afterEach(() => {
    spyLog.mockRestore();
    spyExit.mockRestore();
    mockFetch.mockRestore();
  });

  it("should output the correct plugin version when status command is run", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ version: expectedVersion }),
    });

    const registeredActions: Record<string, Function> = {};
    let lastCommand = "";

    const mockCliCtx = {
      program: {
        command: jest.fn().mockImplementation((name) => {
          lastCommand = name;
          return mockCliCtx.program;
        }),
        description: jest.fn().mockReturnThis(),
        option: jest.fn().mockReturnThis(),
        action: jest.fn().mockImplementation((fn) => {
          if (lastCommand) {
            registeredActions[lastCommand] = fn;
          }
          return mockCliCtx.program;
        }),
      },
      logger: {
        info: jest.fn(),
        warn: jest.fn(),
        debug: jest.fn(),
      },
      workspaceDir: "/mock/workspace",
    };

    let cliCallback: ((ctx: any) => void) | undefined;

    const api = {
      id: "cognee-openclaw",
      name: "Memory (Cognee)",
      pluginConfig: {},
      logger: {
        info: jest.fn(),
        warn: jest.fn(),
        debug: jest.fn(),
      },
      registerMemoryFlushPlan: jest.fn(),
      registerCli: jest.fn().mockImplementation((cb) => {
        cliCallback = cb;
      }),
      registerService: jest.fn(),
      on: jest.fn(),
    };

    plugin.register(api as any);

    expect(api.registerCli).toHaveBeenCalledTimes(1);
    expect(cliCallback).toBeDefined();

    cliCallback!(mockCliCtx);

    const statusActionFn = registeredActions["status"];
    expect(statusActionFn).toBeDefined();

    await statusActionFn();

    expect(spyLog).toHaveBeenCalledWith(`Plugin Version: ${expectedVersion}`);
  });

  it("should display update warning if a newer version is available", async () => {
    mockFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ version: "9999.0.0" }),
    });

    const registeredActions: Record<string, Function> = {};
    let lastCommand = "";

    const mockCliCtx = {
      program: {
        command: jest.fn().mockImplementation((name) => {
          lastCommand = name;
          return mockCliCtx.program;
        }),
        description: jest.fn().mockReturnThis(),
        option: jest.fn().mockReturnThis(),
        action: jest.fn().mockImplementation((fn) => {
          if (lastCommand) {
            registeredActions[lastCommand] = fn;
          }
          return mockCliCtx.program;
        }),
      },
      logger: {
        info: jest.fn(),
        warn: jest.fn(),
        debug: jest.fn(),
      },
      workspaceDir: "/mock/workspace",
    };

    let cliCallback: ((ctx: any) => void) | undefined;

    const api = {
      id: "cognee-openclaw",
      name: "Memory (Cognee)",
      pluginConfig: {},
      logger: {
        info: jest.fn(),
        warn: jest.fn(),
        debug: jest.fn(),
      },
      registerMemoryFlushPlan: jest.fn(),
      registerCli: jest.fn().mockImplementation((cb) => {
        cliCallback = cb;
      }),
      registerService: jest.fn(),
      on: jest.fn(),
    };

    plugin.register(api as any);
    cliCallback!(mockCliCtx);

    const statusActionFn = registeredActions["status"];
    expect(statusActionFn).toBeDefined();

    await statusActionFn();

    expect(spyLog).toHaveBeenCalledWith(`Plugin Version: ${expectedVersion}`);
    expect(spyLog).toHaveBeenCalledWith(`Update available: 9999.0.0 (current: ${expectedVersion})`);
    expect(spyLog).toHaveBeenCalledWith("Run 'npm install -g @cognee/cognee-openclaw' to update.");
  });
});
