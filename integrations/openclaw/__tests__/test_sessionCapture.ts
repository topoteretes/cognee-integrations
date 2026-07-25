import plugin from "../src/plugin";
import { CogneeHttpClient } from "../src/client";
import { spawnExitWatcher } from "../src/server";

jest.mock("../src/client");
jest.mock("../src/server", () => ({
  bootServerIfNeeded: jest.fn(async () => {}),
  waitForServerHealth: jest.fn(async () => {}),
  isLocalUrl: jest.fn(() => true),
  resolveOrMintApiKey: jest.fn(async () => "test-api-key"),
  spawnExitWatcher: jest.fn(async () => {}),
  exitWatcherPidfilePath: jest.fn((name: string) => `/tmp/exit-watchers/${name}.pid`),
}));

const mockRememberEntry = jest.fn(async (_params: unknown): Promise<{ entryId?: string }> => ({ entryId: "e1" }));
const mockRegisterAgent = jest.fn(async (_params: unknown) => ({ ok: true, connectionId: "c1" }));
const mockUnregisterAgent = jest.fn(async (_params: unknown) => ({ ok: true, activeAgents: 0 }));
const mockImprove = jest.fn(async (_params: unknown): Promise<{ status?: string }> => ({ status: "ok" }));
const mockHealth = jest.fn(async () => ({ status: "ok" }));

function resetMockImplementations(): void {
  mockRememberEntry.mockImplementation(async () => ({ entryId: "e1" }));
  mockRegisterAgent.mockImplementation(async () => ({ ok: true, connectionId: "c1" }));
  mockUnregisterAgent.mockImplementation(async () => ({ ok: true, activeAgents: 0 }));
  mockImprove.mockImplementation(async () => ({ status: "ok" }));
  mockHealth.mockImplementation(async () => ({ status: "ok" }));
}

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
      autoRecall: false,
      enableSessions: true,
      captureSession: true,
      datasetName: "testds",
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
    for (const fn of handlers.get(name) ?? []) {
      await fn(event, ctx);
    }
  };
  return { api, emit };
}

/** Let fire-and-forget promise chains settle. */
async function flush(rounds = 10): Promise<void> {
  for (let i = 0; i < rounds; i++) {
    await new Promise((r) => setTimeout(r, 0));
  }
}

beforeEach(() => {
  jest.clearAllMocks();
  resetMockImplementations();
  (CogneeHttpClient as unknown as jest.Mock).mockImplementation(() => ({
    rememberEntry: mockRememberEntry,
    registerAgent: mockRegisterAgent,
    unregisterAgent: mockUnregisterAgent,
    improve: mockImprove,
    health: mockHealth,
    setApiKey: jest.fn(),
  }));
});

describe("session capture (traces + QA)", () => {
  it("stores a tool call as a TraceEntry", async () => {
    const { emit } = createApi();

    await emit("after_tool_call", {
      toolName: "exec",
      params: { command: "ls -la" },
      result: "total 0",
    }, { agentId: "will", sessionId: "s1" });
    await flush();

    expect(mockRememberEntry).toHaveBeenCalledTimes(1);
    const call = mockRememberEntry.mock.calls[0]![0] as unknown as {
      datasetName: string; sessionId: string; entry: Record<string, unknown>;
    };
    expect(call.datasetName).toBe("testds");
    expect(call.sessionId).toBe("open_claw_s1");
    expect(call.entry.type).toBe("trace");
    expect(call.entry.origin_function).toBe("exec");
    expect(call.entry.status).toBe("success");
    expect(call.entry.method_return_value).toBe("total 0");
    expect(call.entry.generate_feedback_with_llm).toBe(false);
  });

  it("marks failed tool calls with status=error", async () => {
    const { emit } = createApi();

    await emit("after_tool_call", {
      toolName: "exec",
      params: { command: "false" },
      error: "exit 1",
    }, { agentId: "will", sessionId: "s1" });
    await flush();

    const call = mockRememberEntry.mock.calls[0]![0] as unknown as { entry: Record<string, unknown> };
    expect(call.entry.status).toBe("error");
    expect(call.entry.error_message).toBe("exit 1");
  });

  it("skips self-referential cognee commands", async () => {
    const { emit } = createApi();

    await emit("after_tool_call", {
      toolName: "exec",
      params: { command: "curl http://localhost:8011/api/v1/cognee-thing" },
      result: "ok",
    }, { agentId: "will", sessionId: "s1" });
    await flush();

    expect(mockRememberEntry).not.toHaveBeenCalled();
  });

  it("truncates oversized tool output", async () => {
    const { emit } = createApi();

    await emit("after_tool_call", {
      toolName: "read",
      params: { path: "big.txt" },
      result: "x".repeat(20_000),
    }, { agentId: "will", sessionId: "s1" });
    await flush();

    const call = mockRememberEntry.mock.calls[0]![0] as unknown as { entry: Record<string, unknown> };
    const returned = call.entry.method_return_value as string;
    expect(returned.length).toBeLessThan(9_000);
    expect(returned.endsWith("…[truncated]")).toBe(true);
  });

  it("pairs a prompt with the first llm_output as a QAEntry", async () => {
    const { emit } = createApi();

    await emit("before_prompt_build", { prompt: "what is the weather" }, { agentId: "will", sessionId: "s1" });
    await flush();
    mockRememberEntry.mockClear(); // ignore any registration-adjacent writes

    await emit("llm_output", { assistantTexts: ["sunny today"] }, { agentId: "will", sessionId: "s1" });
    await flush();

    expect(mockRememberEntry).toHaveBeenCalledTimes(1);
    const call = mockRememberEntry.mock.calls[0]![0] as unknown as { entry: Record<string, unknown> };
    expect(call.entry.type).toBe("qa");
    expect(call.entry.question).toBe("what is the weather");
    expect(call.entry.answer).toBe("sunny today");

    // A second llm_output without a new prompt must NOT create a second QA row.
    await emit("llm_output", { assistantTexts: ["follow-up chunk"] }, { agentId: "will", sessionId: "s1" });
    await flush();
    expect(mockRememberEntry).toHaveBeenCalledTimes(1);
  });

  it("registers the session and passes dataset + session to the exit-watcher", async () => {
    const { emit } = createApi();

    await emit("before_prompt_build", { prompt: "hello there" }, { agentId: "will", sessionId: "s1" });
    await flush();

    expect(mockRegisterAgent).toHaveBeenCalledTimes(1);
    expect(spawnExitWatcher).toHaveBeenCalledWith(expect.objectContaining({
      agentSessionName: "s1-will",
      datasetName: "testds",
      cogneeSessionId: "open_claw_s1",
    }));
  });
});

describe("session_end final chain", () => {
  it("improves before unregistering and returns without blocking", async () => {
    const { emit } = createApi();

    // gateway_start resolves serviceReady, which the final chain awaits.
    await emit("gateway_start", { port: 1 }, {});
    await flush();
    await emit("before_prompt_build", { prompt: "hello there" }, { agentId: "will", sessionId: "s1" });
    await flush();

    await emit("session_end", { sessionId: "s1", messageCount: 1 }, { agentId: "will", sessionId: "s1" });
    await flush(30);

    expect(mockImprove).toHaveBeenCalledTimes(1);
    const improveArg = mockImprove.mock.calls[0]![0] as unknown as { datasetName: string; sessionIds: string[] };
    expect(improveArg.datasetName).toBe("testds");
    expect(improveArg.sessionIds).toEqual(["open_claw_s1"]);
    expect(mockUnregisterAgent).toHaveBeenCalledWith({ agentSessionName: "s1-will" });

    const improveOrder = mockImprove.mock.invocationCallOrder[0]!;
    const unregisterOrder = mockUnregisterAgent.mock.invocationCallOrder[0]!;
    expect(improveOrder).toBeLessThan(unregisterOrder);
  });

  it("still unregisters when improve keeps failing", async () => {
    mockImprove.mockRejectedValue(new Error("server gone"));
    jest.useFakeTimers();
    try {
      const { emit } = createApi();

      await emit("gateway_start", { port: 1 }, {});
      await emit("before_prompt_build", { prompt: "hello there" }, { agentId: "will", sessionId: "s1" });
      // Small stepped advances let real I/O promises (state loading) settle
      // under fake timers before and during the retry window.
      for (let i = 0; i < 10; i++) await jest.advanceTimersByTimeAsync(100);

      await emit("session_end", { sessionId: "s1", messageCount: 1 }, { agentId: "will", sessionId: "s1" });
      // 3 attempts with 10s between them.
      for (let i = 0; i < 40; i++) await jest.advanceTimersByTimeAsync(1_000);

      expect(mockImprove).toHaveBeenCalledTimes(3);
      expect(mockUnregisterAgent).toHaveBeenCalledTimes(1);
    } finally {
      jest.useRealTimers();
      resetMockImplementations();
    }
  });

  it("unregisters even when gateway_start never fired (agent-scoped plugin instance)", async () => {
    jest.useFakeTimers();
    try {
      const { emit } = createApi();

      // NO gateway_start — serviceReady never resolves in this instance.
      await emit("before_prompt_build", { prompt: "hello there" }, { agentId: "will", sessionId: "s1" });
      for (let i = 0; i < 10; i++) await jest.advanceTimersByTimeAsync(100);

      await emit("session_end", { sessionId: "s1", messageCount: 1 }, { agentId: "will", sessionId: "s1" });
      // Past the 5s serviceReady timeout.
      for (let i = 0; i < 10; i++) await jest.advanceTimersByTimeAsync(1_000);

      expect(mockImprove).toHaveBeenCalledTimes(1);
      expect(mockUnregisterAgent).toHaveBeenCalledWith({ agentSessionName: "s1-will" });
    } finally {
      jest.useRealTimers();
    }
  });

  it("resolves the API key lazily when gateway_start never fired", async () => {
    const { resolveOrMintApiKey } = jest.requireMock("../src/server") as { resolveOrMintApiKey: jest.Mock };
    const { emit } = createApi();

    // NO gateway_start — resolvedApiKey is unset in this instance.
    await emit("before_prompt_build", { prompt: "hello there" }, { agentId: "will", sessionId: "s1" });
    await flush();

    expect(resolveOrMintApiKey).toHaveBeenCalled();
    expect(spawnExitWatcher).toHaveBeenCalledWith(expect.objectContaining({
      agentSessionName: "s1-will",
      apiKey: "test-api-key",
    }));
  });

  it("deduplicates a double session_end", async () => {
    const { emit } = createApi();

    await emit("gateway_start", { port: 1 }, {});
    await flush();
    await emit("before_prompt_build", { prompt: "hello there" }, { agentId: "will", sessionId: "s1" });
    await flush();

    await Promise.all([
      emit("session_end", { sessionId: "s1", messageCount: 1 }, { agentId: "will", sessionId: "s1" }),
      emit("session_end", { sessionId: "s1", messageCount: 1 }, { agentId: "will", sessionId: "s1" }),
    ]);
    await flush(30);

    expect(mockImprove).toHaveBeenCalledTimes(1);
    expect(mockUnregisterAgent).toHaveBeenCalledTimes(1);
  });
});
