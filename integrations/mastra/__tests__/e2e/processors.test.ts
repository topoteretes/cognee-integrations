/**
 * e2e tier: `src/processors.ts` exercised against a real `node:http` mock
 * server, through the actual `Processor` interface methods with hand-built
 * args — not a full `Agent`/model call.
 *
 * Covers the four hard invariants: fail-open on any cognee failure, recall
 * never retries or exceeds `budgetMs`, no secret in a debug log line, and
 * `cognee_forget` gating (covered instead by `tools.test.ts`).
 */

import { jest } from "@jest/globals";
import { RequestContext } from "@mastra/core/request-context";
import { MessageList } from "@mastra/core/agent/message-list";
import type { MastraDBMessage } from "@mastra/core/agent/message-list";
import type { ProcessInputArgs, ProcessOutputResultArgs, OutputResult } from "@mastra/core/processors";
import type { AgentConfig } from "@mastra/core/agent";

import { CogneeInputProcessor, CogneeOutputProcessor, createCogneeProcessors } from "../../src/processors.js";
import { withCognee } from "../../src/with-cognee.js";
import { CircuitBreaker } from "../../src/breaker.js";
import { startMockCognee, type MockCognee } from "../test-utils/mock-cognee.js";
import { DATASET_ID, RECALL_RESPONSE, RECALL_RESPONSE_EMPTY } from "../test-utils/fixtures.js";
import type { CogneeMastraConfig } from "../../src/types.js";

// ---------------------------------------------------------------------------
// Test harness — a stubbed Mastra turn, not a full Agent/model call.
// ---------------------------------------------------------------------------

/**
 * Every `Processor` method's required `abort` — throws so an accidental call surfaces loudly in a
 * test rather than silently truncating the turn.
 */
function abortFn(): (reason?: string) => never {
  return (reason?: string): never => {
    throw new Error(`processor called abort(): ${reason ?? "(no reason given)"}`);
  };
}

/**
 * Builds the `RequestContext` a turn carries `threadId`/`resourceId` in
 * (`requestContext.set('MastraMemory', {...})`); omitting both reproduces "no Memory attached",
 * where both processors must no-op.
 */
function memoryRequestContext(ids: { threadId?: string; resourceId?: string } = {}): RequestContext {
  const ctx = new RequestContext();
  if (ids.threadId !== undefined || ids.resourceId !== undefined) {
    ctx.set("MastraMemory", {
      thread: ids.threadId !== undefined ? { id: ids.threadId } : undefined,
      resourceId: ids.resourceId,
    });
  }
  return ctx;
}

function baseConfig(mock: MockCognee, overrides: CogneeMastraConfig = {}): CogneeMastraConfig {
  return {
    baseUrl: mock.url,
    apiKey: "test-api-key",
    dataset: "mastra-processors-test",
    ...overrides,
  };
}

/**
 * Poll until `predicate()` is true, for asserting on the output processor's fire-and-forget write
 * (never awaited by `processOutputResult` itself).
 */
async function waitFor(predicate: () => boolean, timeoutMs = 2000, intervalMs = 5): Promise<void> {
  const start = Date.now();
  while (!predicate()) {
    if (Date.now() - start > timeoutMs) {
      throw new Error(`waitFor: condition not met within ${timeoutMs}ms`);
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

let mock: MockCognee;
let breakerDir: string;
let breakerPath: string;

beforeEach(async () => {
  const { mkdtemp } = await import("node:fs/promises");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  breakerDir = await mkdtemp(join(tmpdir(), "cognee-mastra-processors-test-"));
  breakerPath = join(breakerDir, "recall-breaker.json");
});

afterEach(async () => {
  if (mock) {
    await mock.close();
    mock = undefined as unknown as MockCognee;
  }
  const { rm } = await import("node:fs/promises");
  await rm(breakerDir, { recursive: true, force: true });
});

// ---------------------------------------------------------------------------
// CogneeInputProcessor
// ---------------------------------------------------------------------------

describe("CogneeInputProcessor", () => {
  function buildArgs(opts: {
    threadId?: string;
    resourceId?: string;
    userText?: string;
  }): { args: ProcessInputArgs; messageList: MessageList } {
    const messageList = new MessageList({ threadId: opts.threadId, resourceId: opts.resourceId });
    if (opts.userText !== undefined) messageList.add(opts.userText, "user");
    const args: ProcessInputArgs = {
      messages: messageList.get.input.db(),
      messageList,
      systemMessages: [],
      state: {},
      abort: abortFn(),
      requestContext: memoryRequestContext(opts),
      retryCount: 0,
    };
    return { args, messageList };
  }

  it("injects a context block as a 'cognee'-tagged system message on a recall hit", async () => {
    mock = await startMockCognee();
    const { input } = createCogneeProcessors({ ...baseConfig(mock), breaker: new CircuitBreaker({ path: breakerPath }) });

    const { args, messageList } = buildArgs({
      threadId: "thread-1",
      resourceId: "resource-1",
      userText: "What do you know about my display preferences?",
    });

    const result = await input.processInput(args);
    expect(result).toBe(messageList);

    const tagged = messageList.getSystemMessages("cognee");
    expect(tagged).toHaveLength(1);
    expect(String(tagged[0]!.content)).toContain("Relevant memory from cognee:");
    expect(String(tagged[0]!.content)).toContain("dark mode");

    const recallReq = mock.requests.find((r) => r.path === "/api/v1/recall");
    expect(recallReq).toBeDefined();
    const body = recallReq!.json as Record<string, unknown>;
    expect(body.search_type).toBe("CHUNKS");
    expect(body.only_context).toBe(true);
    expect(body.datasets).toEqual(["mastra-processors-test"]);
    expect(body.session_id).toBe("mastra_thread-1");
    expect(body.node_name).toEqual(["resource:resource-1"]);
  });

  it("injects nothing on an empty recall result", async () => {
    mock = await startMockCognee({ routes: { "POST /api/v1/recall": () => ({ status: 200, body: RECALL_RESPONSE_EMPTY }) } });
    const { input } = createCogneeProcessors({ ...baseConfig(mock), breaker: new CircuitBreaker({ path: breakerPath }) });

    const { args, messageList } = buildArgs({ threadId: "t1", resourceId: "r1", userText: "tell me about my prior conversations" });
    const result = await input.processInput(args);

    expect(result).toBe(messageList);
    expect(messageList.getSystemMessages("cognee")).toHaveLength(0);
  });

  it("injects nothing and does not throw when the server 500s (fail-open, hard invariant #1)", async () => {
    mock = await startMockCognee({ routes: { "POST /api/v1/recall": () => ({ status: 500, body: { detail: "boom" } }) } });
    const breaker = new CircuitBreaker({ path: breakerPath, threshold: 1 });
    const { input } = createCogneeProcessors({ ...baseConfig(mock), breaker });

    const { args, messageList } = buildArgs({ threadId: "t1", resourceId: "r1", userText: "a sufficiently long query string" });

    await expect(input.processInput(args)).resolves.toBe(messageList);
    expect(messageList.getSystemMessages("cognee")).toHaveLength(0);
    // the failure was recorded against the breaker (threshold: 1 -> tripped by this one call)
    expect(await breaker.isOpen()).toBe(true);
  });

  it("does NOT record a breaker failure for a deterministic 4xx (bad key / validation error)", async () => {
    mock = await startMockCognee({ routes: { "POST /api/v1/recall": () => ({ status: 422, body: { detail: "invalid query" } }) } });
    const breaker = new CircuitBreaker({ path: breakerPath, threshold: 1 });
    const { input } = createCogneeProcessors({ ...baseConfig(mock), breaker });

    // Five consecutive 4xx failures must not trip the breaker (threshold: 1
    // here) — recordFailure() is skipped for 4xx because a bad key or
    // malformed query is a config problem, not a cognee outage; counting it
    // would blackout recall for 120s over something the breaker can't fix.
    for (let i = 0; i < 5; i++) {
      const { args, messageList } = buildArgs({ threadId: "t1", resourceId: "r1", userText: "a sufficiently long query string" });
      await expect(input.processInput(args)).resolves.toBe(messageList);
    }

    expect(await breaker.isOpen()).toBe(false);
  });

  it("injects nothing and does not throw when the server hangs past budgetMs (hard invariant #2)", async () => {
    mock = await startMockCognee({ routes: { "POST /api/v1/recall": () => new Promise(() => {}) } });
    // timeoutMs is set larger than budgetMs here to prove the budgetMs
    // backstop fires independently of the client's own AbortSignal timeout
    // (kept small, not the client's 2500ms default, so this test's one
    // still-in-flight background request settles quickly instead of
    // holding a real timer open for seconds after the assertions below).
    const { input } = createCogneeProcessors({
      ...baseConfig(mock, { recall: { budgetMs: 150, timeoutMs: 400 } }),
      breaker: new CircuitBreaker({ path: breakerPath }),
    });

    const { args, messageList } = buildArgs({ threadId: "t1", resourceId: "r1", userText: "a sufficiently long query string" });

    const startedAt = Date.now();
    const result = await input.processInput(args);
    const elapsedMs = Date.now() - startedAt;

    expect(result).toBe(messageList);
    expect(messageList.getSystemMessages("cognee")).toHaveLength(0);
    // adds < budgetMs + 100ms to the turn
    expect(elapsedMs).toBeLessThan(150 + 100);
  });

  it("never retries the recall path, even on a 500 (hard invariant #2)", async () => {
    mock = await startMockCognee({ failFirstN: 5 }); // every request in this test would fail if the client kept trying
    const { input } = createCogneeProcessors({ ...baseConfig(mock), breaker: new CircuitBreaker({ path: breakerPath }) });

    const { args, messageList } = buildArgs({ threadId: "t1", resourceId: "r1", userText: "a sufficiently long query string" });
    await input.processInput(args);

    const recallAttempts = mock.requests.filter((r) => r.path === "/api/v1/recall");
    expect(recallAttempts).toHaveLength(1); // a retrying client would show 2+ attempts here
  });

  it("skips recall when the query is shorter than minQueryLength", async () => {
    mock = await startMockCognee();
    const { input } = createCogneeProcessors({ ...baseConfig(mock), breaker: new CircuitBreaker({ path: breakerPath }) });

    const { args, messageList } = buildArgs({ threadId: "t1", resourceId: "r1", userText: "ok" }); // shorter than default minQueryLength (8)
    const result = await input.processInput(args);

    expect(result).toBe(messageList);
    expect(mock.requests).toHaveLength(0);
  });

  it("skips recall when config.recall.enabled is false", async () => {
    mock = await startMockCognee();
    const { input } = createCogneeProcessors({
      ...baseConfig(mock, { recall: { enabled: false } }),
      breaker: new CircuitBreaker({ path: breakerPath }),
    });

    const { args } = buildArgs({ threadId: "t1", resourceId: "r1", userText: "a sufficiently long query string" });
    await input.processInput(args);

    expect(mock.requests).toHaveLength(0);
  });

  it("skips recall when the circuit breaker is already open", async () => {
    mock = await startMockCognee();
    const breaker = new CircuitBreaker({ path: breakerPath, threshold: 1, cooldownMs: 60_000 });
    await breaker.recordFailure("pre-tripped for this test");
    expect(await breaker.isOpen()).toBe(true);

    const { input } = createCogneeProcessors({ ...baseConfig(mock), breaker });

    const { args, messageList } = buildArgs({ threadId: "t1", resourceId: "r1", userText: "a sufficiently long query string" });
    const result = await input.processInput(args);

    expect(result).toBe(messageList);
    expect(mock.requests).toHaveLength(0); // breaker open -> zero network calls, not even one
  });

  it("no-ops (does not call the network) when no Memory context is present", async () => {
    mock = await startMockCognee();
    const { input } = createCogneeProcessors({ ...baseConfig(mock), breaker: new CircuitBreaker({ path: breakerPath }) });

    // No threadId/resourceId at all -> memoryRequestContext() sets nothing ->
    // parseMemoryRequestContext() returns null.
    const { args } = buildArgs({ userText: "a sufficiently long query string" });
    await input.processInput(args);

    expect(mock.requests).toHaveLength(0);
  });

  it("does not appear in any log line even with debug: true (hard invariant #3)", async () => {
    mock = await startMockCognee({ requireAuth: "apiKey", apiKey: "shh-secret-value" });
    const secret = "shh-secret-value";
    const logSpy = jest.spyOn(console, "debug").mockImplementation(() => {});
    try {
      const { input } = createCogneeProcessors({
        ...baseConfig(mock, { apiKey: secret, debug: true }),
        breaker: new CircuitBreaker({ path: breakerPath }),
      });

      const { args } = buildArgs({ threadId: "t1", resourceId: "r1", userText: "a sufficiently long query string" });
      await input.processInput(args);

      const loggedText = logSpy.mock.calls.map((call) => call.join(" ")).join("\n");
      expect(loggedText).not.toContain(secret);
    } finally {
      logSpy.mockRestore();
    }
  });

  it("honours COGNEE_DATASET from the environment when config.dataset is not set explicitly", async () => {
    // createCogneeProcessors()/withCognee() route their config through
    // resolveConfig(), so a missing config.dataset falls back to
    // COGNEE_DATASET from the environment (mirrors tools.test.ts's
    // env-precedence coverage for the tools path).
    mock = await startMockCognee();
    const originalDataset = process.env.COGNEE_DATASET;
    try {
      process.env.COGNEE_DATASET = "mastra-env-dataset";
      // No `dataset` field here — only baseUrl/apiKey/breaker, so the
      // dataset used can only have come from the env var.
      const { input } = createCogneeProcessors({
        baseUrl: mock.url,
        apiKey: "test-api-key",
        breaker: new CircuitBreaker({ path: breakerPath }),
      });

      const { args } = buildArgs({ threadId: "t1", resourceId: "r1", userText: "a sufficiently long query string" });
      await input.processInput(args);

      const recallReq = mock.requests.find((r) => r.path === "/api/v1/recall")!.json as Record<string, unknown>;
      expect(recallReq.datasets).toEqual(["mastra-env-dataset"]);
    } finally {
      if (originalDataset === undefined) delete process.env.COGNEE_DATASET;
      else process.env.COGNEE_DATASET = originalDataset;
    }
  });
});

// ---------------------------------------------------------------------------
// CogneeOutputProcessor
// ---------------------------------------------------------------------------

describe("CogneeOutputProcessor", () => {
  function buildArgs(opts: {
    threadId?: string;
    resourceId?: string;
    userText?: string;
    assistantText?: string;
  }): { args: ProcessOutputResultArgs; messageList: MessageList; responseMessages: MastraDBMessage[] } {
    const messageList = new MessageList({ threadId: opts.threadId, resourceId: opts.resourceId });
    if (opts.userText !== undefined) messageList.add(opts.userText, "user");
    if (opts.assistantText !== undefined) {
      messageList.add({ role: "assistant", content: opts.assistantText }, "response");
    }
    const responseMessages = messageList.get.response.db();
    const result: OutputResult = {
      text: opts.assistantText ?? "",
      usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 },
      finishReason: "stop",
      steps: [],
    };
    const args: ProcessOutputResultArgs = {
      messages: responseMessages,
      messageList,
      state: {},
      result,
      abort: abortFn(),
      requestContext: memoryRequestContext(opts),
      retryCount: 0,
    };
    return { args, messageList, responseMessages };
  }

  it("writes the turn once via rememberEntry, honouring write.mode: 'always' (default)", async () => {
    mock = await startMockCognee();
    const { output } = createCogneeProcessors(baseConfig(mock));

    const { args, messageList } = buildArgs({
      threadId: "thread-2",
      resourceId: "resource-2",
      userText: "what's my favorite editor theme",
      assistantText: "You prefer dark mode and TypeScript.",
    });

    const result = await output.processOutputResult(args);
    expect(result).toBe(messageList);

    await waitFor(() => mock.requests.some((r) => r.path === "/api/v1/remember/entry"));
    const writes = mock.requests.filter((r) => r.path === "/api/v1/remember/entry");
    expect(writes).toHaveLength(1); // written exactly once for this turn

    const body = writes[0]!.json as { entry: { type: string; question: string; answer: string }; dataset_name?: string; session_id?: string };
    expect(body.entry.type).toBe("qa");
    expect(body.entry.question).toContain("favorite editor theme");
    expect(body.entry.answer).toContain("dark mode");
    expect(body.dataset_name).toBe("mastra-processors-test");
    expect(body.session_id).toBe("mastra_thread-2");
  });

  it("honours write.mode: 'never' — no write is ever attempted", async () => {
    mock = await startMockCognee();
    const { output } = createCogneeProcessors(baseConfig(mock, { write: { mode: "never" } }));

    const { args } = buildArgs({
      threadId: "t1",
      resourceId: "r1",
      userText: "some question",
      assistantText: "some answer",
    });
    await output.processOutputResult(args);

    // give any (incorrect) background write a moment to have shown up
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(mock.requests).toHaveLength(0);
  });

  it("honours write.mode: 'assistant-only' — captures the reply, omits the user's question", async () => {
    mock = await startMockCognee();
    const { output } = createCogneeProcessors(baseConfig(mock, { write: { mode: "assistant-only" } }));

    const { args } = buildArgs({
      threadId: "t1",
      resourceId: "r1",
      userText: "this question must not be captured",
      assistantText: "only this reply should be captured",
    });
    await output.processOutputResult(args);

    await waitFor(() => mock.requests.some((r) => r.path === "/api/v1/remember/entry"));
    const body = mock.requests.find((r) => r.path === "/api/v1/remember/entry")!.json as { entry: { question: string; answer: string } };
    expect(body.entry.question).toBe("");
    expect(body.entry.answer).toContain("only this reply should be captured");
  });

  it("never rejects the turn when the write fails (fail-open, hard invariant #1)", async () => {
    mock = await startMockCognee({
      routes: {
        "POST /api/v1/remember/entry": () => ({ status: 500, body: { detail: "boom" } }),
        "POST /api/v1/remember": () => ({ status: 500, body: { detail: "boom" } }),
      },
    });
    // retries: 0 keeps this test's failing background write short-lived —
    // the write path's own default retry policy is not exempted from
    // retries the way recall's is, so a persistent 500 would otherwise run
    // 3x exponential backoff, leaving the background promise chain running
    // for ~20+ real seconds after the assertions below complete.
    const { output } = createCogneeProcessors(baseConfig(mock, { retries: 0 }));

    const { args, messageList } = buildArgs({
      threadId: "t1",
      resourceId: "r1",
      userText: "a question",
      assistantText: "an answer",
    });

    await expect(output.processOutputResult(args)).resolves.toBe(messageList);

    // the attempt happened (and failed) in the background, but never surfaced
    await waitFor(() => mock.requests.some((r) => r.path === "/api/v1/remember/entry"));
  });

  it("does not write when there is no Memory context (no threadId/resourceId)", async () => {
    mock = await startMockCognee();
    const { output } = createCogneeProcessors(baseConfig(mock));

    const { args } = buildArgs({ userText: "q", assistantText: "a" }); // no threadId/resourceId passed
    await output.processOutputResult(args);

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(mock.requests).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// withCognee()
// ---------------------------------------------------------------------------

describe("withCognee", () => {
  it("appends cognee's processors to an agent config with no existing processors", async () => {
    mock = await startMockCognee();
    // `withCognee<T extends Partial<ProcessorFields>>`'s constraint is
    // checked against this argument's declared type at the call site — a
    // real, load-bearing typecheck (ts-jest's transpile-only mode skips it
    // at test-run time; `tsconfig.test.json`'s separate typecheck step is
    // what catches it). A bare `{ name: "test-agent" }` literal fails the
    // generic's excess-property check; routing through `unknown` to
    // `Partial<AgentConfig>` (a real supertype, not a lie) satisfies it
    // without hand-constructing a fully-shaped `Processor` mock.
    const merged = withCognee({ name: "test-agent" } as unknown as Partial<AgentConfig>, baseConfig(mock));

    expect(Array.isArray(merged.inputProcessors)).toBe(true);
    expect(Array.isArray(merged.outputProcessors)).toBe(true);
    expect((merged.inputProcessors as { id: string }[]).map((p) => p.id)).toEqual(["cognee-input"]);
    expect((merged.outputProcessors as { id: string }[]).map((p) => p.id)).toEqual(["cognee-output"]);
  });

  it("appends after an existing array of processors, preserving order", async () => {
    mock = await startMockCognee();
    const existingInput = { id: "existing-input" } as unknown as { id: string };
    const existingOutput = { id: "existing-output" } as unknown as { id: string };

    const merged = withCognee(
      { inputProcessors: [existingInput], outputProcessors: [existingOutput] } as unknown as Partial<AgentConfig>,
      baseConfig(mock),
    );

    expect((merged.inputProcessors as { id: string }[]).map((p) => p.id)).toEqual(["existing-input", "cognee-input"]);
    expect((merged.outputProcessors as { id: string }[]).map((p) => p.id)).toEqual(["existing-output", "cognee-output"]);
  });

  it("appends after an existing function-shaped processors field, preserving order", async () => {
    mock = await startMockCognee();
    const existingInput = { id: "existing-input" };
    const inputProcessors = async () => [existingInput] as unknown as { id: string }[];

    const merged = withCognee({ inputProcessors } as unknown as Partial<AgentConfig>, baseConfig(mock));

    expect(typeof merged.inputProcessors).toBe("function");
    const resolved = await (merged.inputProcessors as unknown as (ctx: unknown) => Promise<{ id: string }[]>)(
      { requestContext: new RequestContext() },
    );
    expect(resolved.map((p) => p.id)).toEqual(["existing-input", "cognee-input"]);
  });

  it("does not mutate the input agentConfig object", async () => {
    mock = await startMockCognee();
    const original = { name: "test-agent" };
    withCognee(original as unknown as Partial<AgentConfig>, baseConfig(mock));
    expect(original).toEqual({ name: "test-agent" });
  });
});

// ---------------------------------------------------------------------------
// Reference constant sanity (guards against a fixture rename silently
// de-scoping the "dark mode" assertion above)
// ---------------------------------------------------------------------------

it("fixtures sanity: RECALL_RESPONSE's first hit belongs to DATASET_ID", () => {
  expect(RECALL_RESPONSE[0]!.dataset_id).toBe(DATASET_ID);
});
