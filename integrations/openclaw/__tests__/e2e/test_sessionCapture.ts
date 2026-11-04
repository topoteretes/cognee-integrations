import plugin from "../../src/plugin";
import { CogneeHttpClient, CogneeRequestError } from "../../src/client";
import { spawnExitWatcher } from "../../src/server";

jest.mock("../../src/client", () => {
  // Keep the real error class (the plugin reads its status); mock the client.
  const actual = jest.requireActual("../../src/client");
  return { ...actual, CogneeHttpClient: jest.fn() };
});
jest.mock("../../src/server", () => ({
  bootServerIfNeeded: jest.fn(async () => {}),
  waitForServerHealth: jest.fn(async () => {}),
  isLocalUrl: jest.fn(() => true),
  resolveOrMintApiKey: jest.fn(async () => "test-api-key"),
  spawnExitWatcher: jest.fn(async () => {}),
  exitWatcherPidfilePath: jest.fn((name: string) => `/tmp/exit-watchers/${name}.pid`),
  writeExitWatcherBridge: jest.fn(async () => {}),
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

function createApi(pluginConfig: Record<string, unknown> = {}) {
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
      ...pluginConfig,
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

// Captured before any test installs fake timers (which also fake setImmediate).
const realSetImmediate = globalThis.setImmediate;

/**
 * Under fake timers, advance the clock in `stepMs` steps until `done()` or
 * `maxSteps`, yielding to the REAL event loop between steps. The session-end
 * chain awaits real fs I/O (state loading, dataset-state save), which only
 * completes when the loop actually turns — a fixed number of fake advances
 * races a slow CI disk and flakes.
 *
 * One `readFile` alone is several libuv round trips (open/fstat/read/close),
 * each needing its own poll phase, and the chain strings many such calls
 * together — so a single setImmediate per step was not enough on a loaded CI
 * runner and the loop ran out of steps before the chain reached unregister.
 * Each step now yields `yieldsPerStep` real loop turns, and the step cap is
 * generous: the loop exits as soon as `done()` holds, so the cap only matters
 * when the chain is genuinely stuck, and fake-clock advances are cheap.
 */
async function advanceUntil(
  done: () => boolean,
  maxSteps = 300,
  stepMs = 1_000,
  yieldsPerStep = 10,
): Promise<void> {
  for (let i = 0; i < maxSteps && !done(); i++) {
    await jest.advanceTimersByTimeAsync(stepMs);
    for (let j = 0; j < yieldsPerStep && !done(); j++) {
      await new Promise<void>((r) => realSetImmediate(r));
    }
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
  it("does not persist clean or crashed sessions when persistence is disabled", async () => {
    const { emit } = createApi({ persistSessionsAfterEnd: false });

    await emit("gateway_start", { port: 1 }, {});
    await flush();
    await emit("before_prompt_build", { prompt: "hello there" }, { agentId: "will", sessionId: "s1" });
    await flush();

    expect(spawnExitWatcher).toHaveBeenCalledWith(expect.objectContaining({
      datasetName: undefined,
      cogneeSessionId: undefined,
    }));

    await emit("session_end", { sessionId: "s1", messageCount: 1 }, { agentId: "will", sessionId: "s1" });
    await flush(30);

    expect(mockImprove).not.toHaveBeenCalled();
    expect(mockUnregisterAgent).toHaveBeenCalledWith({ agentSessionName: "s1-will" });
  });

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

  it("waits for in-flight capture writes before improving (cold-server race)", async () => {
    // Regression for the nightly live failure: the QA's remember/entry POST was
    // still in flight when session_end's improve arrived, improve bridged an
    // empty session and reported success, and the turn never reached the graph.
    let releaseStore!: () => void;
    const gate = new Promise<void>((r) => { releaseStore = r; });
    mockRememberEntry.mockImplementation(async () => {
      await gate;
      return { entryId: "slow-e1" };
    });
    try {
      const { emit } = createApi();

      await emit("gateway_start", { port: 1 }, {});
      await flush();
      await emit("before_prompt_build", { prompt: "what is the weather" }, { agentId: "will", sessionId: "s1" });
      await flush();
      await emit("llm_output", { assistantTexts: ["sunny today"] }, { agentId: "will", sessionId: "s1" });
      await flush();
      expect(mockRememberEntry).toHaveBeenCalledTimes(1);

      await emit("session_end", { sessionId: "s1", messageCount: 1 }, { agentId: "will", sessionId: "s1" });
      await flush(30);
      // The store has not landed yet, so improve must not have fired.
      expect(mockImprove).not.toHaveBeenCalled();

      releaseStore();
      await flush(30);
      expect(mockImprove).toHaveBeenCalledTimes(1);
      expect(mockRememberEntry.mock.invocationCallOrder[0]!).toBeLessThan(mockImprove.mock.invocationCallOrder[0]!);
      expect(mockUnregisterAgent).toHaveBeenCalledTimes(1);
    } finally {
      resetMockImplementations();
    }
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
      // 3 attempts with 10s between them; the chain's real fs I/O (buffer
      // drain, state loading) needs real loop turns, so step until unregister.
      await advanceUntil(() => mockUnregisterAgent.mock.calls.length > 0);

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
      // Past the 5s serviceReady timeout, then until the chain reaches unregister.
      await advanceUntil(() => mockUnregisterAgent.mock.calls.length > 0);

      expect(mockImprove).toHaveBeenCalledTimes(1);
      expect(mockUnregisterAgent).toHaveBeenCalledWith({ agentSessionName: "s1-will" });
    } finally {
      jest.useRealTimers();
    }
  });

  it("resolves the API key lazily when gateway_start never fired", async () => {
    const { resolveOrMintApiKey } = jest.requireMock("../../src/server") as { resolveOrMintApiKey: jest.Mock };
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

  it("replays turns another plugin instance buffered before improving", async () => {
    // OpenClaw registers several instances in one gateway, sharing one write
    // buffer, and session_end can land on an instance that never saw the
    // session's prompts. That instance's view of the buffer must not be stale.
    const a = createApi();
    const b = createApi();
    await a.emit("gateway_start", { port: 1 }, {});
    await b.emit("gateway_start", { port: 1 }, {});
    await flush(30);

    // B stores a turn; its follow-up drain finds the buffer empty.
    await b.emit("before_prompt_build", { prompt: "first question" }, { agentId: "will", sessionId: "s2" });
    await b.emit("llm_output", { assistantTexts: ["first answer"] }, { agentId: "will", sessionId: "s2" });
    await flush(30);

    // The server goes away; A's turn for s1 is buffered.
    const refused = Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } });
    mockRememberEntry.mockRejectedValueOnce(refused);
    await a.emit("before_prompt_build", { prompt: "what is the weather" }, { agentId: "will", sessionId: "s1" });
    await a.emit("llm_output", { assistantTexts: ["sunny today"] }, { agentId: "will", sessionId: "s1" });
    await flush(30);
    expect(a.api.logger.info).toHaveBeenCalledWith(expect.stringMatching(/qa buffered until the Cognee server is reachable/));

    // The server is back; s1 ends on B.
    mockRememberEntry.mockClear();
    await b.emit("session_end", { sessionId: "s1", messageCount: 1 }, { agentId: "will", sessionId: "s1" });
    await flush(30);

    expect(mockImprove).toHaveBeenCalledTimes(1);
    const replayed = mockRememberEntry.mock.calls.map((c) => c[0] as { sessionId: string; entry: { question?: string } });
    expect(replayed).toContainEqual(expect.objectContaining({ sessionId: "open_claw_s1", entry: expect.objectContaining({ question: "what is the weather" }) }));
    const replayOrder = mockRememberEntry.mock.invocationCallOrder[replayed.findIndex((r) => r.sessionId === "open_claw_s1")]!;
    expect(replayOrder).toBeLessThan(mockImprove.mock.invocationCallOrder[0]!);
  });
});

describe("capture policy", () => {
  const ctx = { agentId: "will", sessionId: "s1" };
  const traces = () => mockRememberEntry.mock.calls.map((c) => (c[0] as { entry: Record<string, unknown> }).entry).filter((e) => e.type === "trace");

  it("skips tools the policy denies, including Tool(prefix:*) on a shell command's first word", async () => {
    const { emit } = createApi({ captureDenyTools: ["exec(git:*)", "web_*"] });
    await emit("after_tool_call", { toolName: "exec", params: { command: "git status" }, result: "clean" }, ctx);
    await emit("after_tool_call", { toolName: "web_fetch", params: { url: "https://x" }, result: "page" }, ctx);
    await emit("after_tool_call", { toolName: "exec", params: { command: "ls" }, result: "a b" }, ctx);
    await flush();
    expect(traces().map((t) => t.method_params)).toEqual([{ command: "ls" }]);
  });

  it("never captures calls touching secret files", async () => {
    const { emit } = createApi();
    await emit("after_tool_call", { toolName: "read", params: { path: "/repo/.env.local" }, result: "API_KEY=abc" }, ctx);
    await emit("after_tool_call", { toolName: "read", params: { path: "/repo/README.md" }, result: "hi" }, ctx);
    await flush();
    expect(traces()).toHaveLength(1);
    expect(traces()[0].method_params).toEqual({ path: "/repo/README.md" });
  });

  it("redacts secrets from traces and from both sides of a QA", async () => {
    const key = "sk-" + "a".repeat(40);
    const { emit } = createApi();
    await emit("after_tool_call", { toolName: "exec", params: { command: `curl -H "Authorization: Bearer ${key}" x` }, result: `token=${key}` }, ctx);
    await emit("before_prompt_build", { prompt: `use this key ${key} for the deploy` }, ctx);
    await emit("llm_output", { assistantTexts: [`ok, stored ${key}`] }, ctx);
    await flush();
    const stored = JSON.stringify(mockRememberEntry.mock.calls.map((c) => c[0]));
    expect(stored).not.toContain(key);
    expect(stored).toContain("[redacted");
  });

  it("fails closed on a pattern it cannot parse: no traces, and says why", async () => {
    const { api, emit } = createApi({ captureTools: ["exec(rg)"] });
    await emit("after_tool_call", { toolName: "exec", params: { command: "ls" }, result: "a" }, ctx);
    await flush();
    expect(traces()).toHaveLength(0);
    expect(api.logger.warn).toHaveBeenCalledWith(expect.stringContaining("NOT being captured"));
  });

  it("keeps redaction off when captureRedact is false", async () => {
    const key = "sk-" + "b".repeat(40);
    const { emit } = createApi({ captureRedact: false });
    await emit("before_prompt_build", { prompt: `key ${key}` }, ctx);
    await emit("llm_output", { assistantTexts: ["noted"] }, ctx);
    await flush();
    expect(JSON.stringify(mockRememberEntry.mock.calls)).toContain(key);
  });
});

describe("write buffer", () => {
  const ctx = { agentId: "will", sessionId: "s1" };
  const refused = () => Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } });

  it("buffers a QA the server never received and replays it at session end, before improve", async () => {
    mockRememberEntry.mockRejectedValueOnce(refused());
    try {
      const { emit } = createApi();
      await emit("gateway_start", { port: 1 }, {});
      await flush();
      await emit("before_prompt_build", { prompt: "what is the weather" }, ctx);
      await emit("llm_output", { assistantTexts: ["sunny today"] }, ctx);
      await flush(30);
      expect(mockRememberEntry).toHaveBeenCalledTimes(1);

      await emit("session_end", { sessionId: "s1", messageCount: 1 }, ctx);
      await flush(60);

      expect(mockRememberEntry).toHaveBeenCalledTimes(2);
      expect(mockRememberEntry.mock.calls[1]![0]).toMatchObject({ sessionId: "open_claw_s1", entry: { type: "qa", answer: "sunny today" } });
      expect(mockImprove).toHaveBeenCalledTimes(1);
      expect(mockRememberEntry.mock.invocationCallOrder[1]!).toBeLessThan(mockImprove.mock.invocationCallOrder[0]!);
    } finally {
      resetMockImplementations();
    }
  });

  it("does not buffer a write that may have been stored (timeout)", async () => {
    mockRememberEntry.mockRejectedValueOnce(Object.assign(new Error("aborted"), { name: "AbortError" }));
    try {
      const { api, emit } = createApi();
      await emit("before_prompt_build", { prompt: "what is the weather" }, ctx);
      await emit("llm_output", { assistantTexts: ["sunny today"] }, ctx);
      await flush(30);
      expect(api.logger.warn).toHaveBeenCalledWith(expect.stringContaining("qa store failed"));
      await emit("after_tool_call", { toolName: "exec", params: { command: "ls" }, result: "a" }, ctx);
      await flush(30);
      // The later successful write found nothing to replay.
      expect(mockRememberEntry).toHaveBeenCalledTimes(2);
    } finally {
      resetMockImplementations();
    }
  });
});

describe("session-end improve on an exhausted LLM budget", () => {
  it("does not retry a 402", async () => {
    mockImprove.mockRejectedValue(new CogneeRequestError(402, '{"error":"budget exhausted"}'));
    try {
      const { api, emit } = createApi();
      await emit("gateway_start", { port: 1 }, {});
      await flush();
      await emit("before_prompt_build", { prompt: "hello there" }, { agentId: "will", sessionId: "s1" });
      await flush();
      await emit("session_end", { sessionId: "s1", messageCount: 1 }, { agentId: "will", sessionId: "s1" });
      await flush(40);
      expect(mockImprove).toHaveBeenCalledTimes(1);
      expect(api.logger.warn).toHaveBeenCalledWith(expect.stringContaining("LLM budget is exhausted"));
      expect(mockUnregisterAgent).toHaveBeenCalledTimes(1);
    } finally {
      resetMockImplementations();
    }
  });
});

describe("capture dataset on session start", () => {
  it("creates the capture dataset before the session's first write, so the server records it on the session", async () => {
    const ensureDataset = jest.fn(async (_name: string) => "ds-created");
    (CogneeHttpClient as unknown as jest.Mock).mockImplementation(() => ({
      rememberEntry: mockRememberEntry,
      registerAgent: mockRegisterAgent,
      unregisterAgent: mockUnregisterAgent,
      improve: mockImprove,
      health: mockHealth,
      setApiKey: jest.fn(),
      ensureDataset,
    }));
    const { emit } = createApi();
    const ctx = { agentId: "will", sessionId: "fresh" };
    await emit("before_prompt_build", { prompt: "first prompt ever" }, ctx);
    await emit("llm_output", { assistantTexts: ["hello"] }, ctx);
    await flush();

    expect(ensureDataset).toHaveBeenCalledWith("testds");
    expect(ensureDataset.mock.invocationCallOrder[0]!).toBeLessThan(mockRememberEntry.mock.invocationCallOrder[0]!);
    expect(ensureDataset.mock.invocationCallOrder[0]!).toBeLessThan(mockRegisterAgent.mock.invocationCallOrder[0]!);
  });
});
