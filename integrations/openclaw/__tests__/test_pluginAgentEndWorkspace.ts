import plugin from "../src/plugin";

const mockRegisterMemoryFlushPlan = jest.fn();

function createApi() {
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
    runtime: {},
    logger: {
      info: jest.fn(),
      warn: jest.fn(),
      debug: jest.fn(),
    },
    registerMemoryFlushPlan: mockRegisterMemoryFlushPlan,
    registerCli: jest.fn(),
    registerService: jest.fn(),
    on: jest.fn(),
  };

  plugin.register(api as never);
  return api;
}

describe("cognee-openclaw memory flush registration", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it("registers a memory flush plan resolver", () => {
    createApi();

    expect(mockRegisterMemoryFlushPlan).toHaveBeenCalledTimes(1);
    const resolver = mockRegisterMemoryFlushPlan.mock.calls[0]?.[0] as
      | ((params?: { nowMs?: number }) => { relativePath: string } | null)
      | undefined;
    expect(resolver).toBeDefined();
    expect(resolver?.({ nowMs: Date.UTC(2026, 3, 3, 0, 0, 0) })?.relativePath).toBe(
      "memory/2026-04-03.md",
    );
  });
});
