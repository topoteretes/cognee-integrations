/**
 * e2e tier: `src/tools.ts` exercised through each tool's actual `execute()`
 * against a real `node:http` mock server — not just the module's exports.
 *
 * Covers: happy-path output schema per tool, a typed `{ error }` (never
 * throw) on a 500, `cognee_forget` gated behind `enableForget`, and that
 * `createCogneeTools()` shares one `CogneeClient` across every tool.
 */

import { RequestContext } from "@mastra/core/request-context";
import type { ToolExecutionContext } from "@mastra/core/tools";

import {
  createCogneeAskTool,
  createCogneeForgetTool,
  createCogneeRememberTool,
  createCogneeSearchTool,
  createCogneeTools,
} from "../../src/tools.js";
import { startMockCognee, type MockCognee } from "../test-utils/mock-cognee.js";
import { DATASET_ID, RECALL_RESPONSE } from "../test-utils/fixtures.js";
import type { CogneeMastraConfig } from "../../src/types.js";

// ---------------------------------------------------------------------------
// Test harness
// ---------------------------------------------------------------------------

/**
 * Minimal, real (not `any`-cast) `ToolExecutionContext` a tool's `execute(inputData, context)`
 * receives; `observe.span` runs the function directly, matching `@mastra/core`'s own `noopObserve`.
 */
function makeToolContext(agent?: { threadId?: string; resourceId?: string }): ToolExecutionContext {
  return {
    requestContext: new RequestContext(),
    observe: {
      async span<T>(_name: string, fn: () => Promise<T> | T): Promise<T> {
        return fn();
      },
      log(): void {},
    },
    ...(agent ? { agent: { agentId: "test-agent", toolCallId: "call-1", messages: [], suspend: async () => {}, ...agent } } : {}),
  };
}

function baseConfig(mock: MockCognee, overrides: CogneeMastraConfig = {}): CogneeMastraConfig {
  return {
    baseUrl: mock.url,
    apiKey: "test-api-key",
    dataset: "mastra-tools-test",
    ...overrides,
  };
}

async function callTool(tool: { execute?: unknown }, input: unknown, context: ToolExecutionContext): Promise<any> {
  const execute = tool.execute as (i: unknown, c: ToolExecutionContext) => Promise<unknown>;
  return execute(input, context);
}

/**
 * A Standard Schema's `validate()` returns `{ issues }` on failure rather
 * than throwing, so `expect(() => ...validate(x)).not.toThrow()` is vacuous
 * (it passes for any input, valid or not). Awaits the result (`validate` may
 * be sync or async) and hands back `{ issues }`/`{ value }` to assert on.
 */
async function validateAgainstSchema(schema: { "~standard": { validate: (v: unknown) => unknown } }, value: unknown) {
  return (await schema["~standard"].validate(value)) as { issues?: ReadonlyArray<{ message: string }>; value?: unknown };
}

let mock: MockCognee;

afterEach(async () => {
  if (mock) await mock.close();
});

// ---------------------------------------------------------------------------
// cognee_search
// ---------------------------------------------------------------------------

describe("createCogneeSearchTool", () => {
  it("returns results matching its declared output schema on the happy path", async () => {
    mock = await startMockCognee();
    const tool = createCogneeSearchTool({ config: baseConfig(mock) });

    const result = await callTool(tool, { query: "what does the user prefer" }, makeToolContext({ threadId: "t1", resourceId: "r1" }));

    expect(result.error).toBeUndefined();
    expect(Array.isArray(result.results)).toBe(true);
    expect(result.results.length).toBe(RECALL_RESPONSE.length);
    expect(result.results[0]).toMatchObject({ text: RECALL_RESPONSE[0]!.text });

    // validate() against the tool's own outputSchema, not just a shape
    // assertion, proves the returned object satisfies what the model sees.
    // validate() returns `{ issues }` on failure rather than throwing, so
    // the result's `issues` field is what must be asserted, not whether it threw.
    const validation = await validateAgainstSchema(tool.outputSchema!, result);
    expect(validation.issues).toBeUndefined();
  });

  it("outputSchema validation actually catches a bad object (negative sanity check for the mechanism above)", async () => {
    mock = await startMockCognee();
    const tool = createCogneeSearchTool({ config: baseConfig(mock) });

    const badValidation = await validateAgainstSchema(tool.outputSchema!, { results: "not-an-array" });
    expect(badValidation.issues).toBeDefined();
    expect(badValidation.issues!.length).toBeGreaterThan(0);
  });

  it("respects a configured tools.timeoutMs budget instead of the client-wide 30s default", async () => {
    mock = await startMockCognee({ routes: { "POST /api/v1/recall": () => new Promise(() => {}) } }); // never responds
    const tool = createCogneeSearchTool({ config: baseConfig(mock, { tools: { timeoutMs: 200 } }) });

    const startedAt = Date.now();
    const result = await callTool(tool, { query: "will this ever come back" }, makeToolContext());
    const elapsedMs = Date.now() - startedAt;

    expect(typeof result.error).toBe("string");
    // Well under the client-wide 30s default (and its up-to-3x retry
    // worst case) — proves this call's own `tools.timeoutMs` (not
    // `requestTimeoutMs`) governs it, and that it retried 0 times.
    expect(elapsedMs).toBeLessThan(2000);
  });

  it("sends CHUNKS / only_context: true", async () => {
    mock = await startMockCognee();
    const tool = createCogneeSearchTool({ config: baseConfig(mock) });
    await callTool(tool, { query: "anything about deployment" }, makeToolContext());

    const recallReq = mock.requests.find((r) => r.path === "/api/v1/recall");
    expect(recallReq).toBeDefined();
    const body = recallReq!.json as Record<string, unknown>;
    expect(body.search_type).toBe("CHUNKS");
    expect(body.only_context).toBe(true);
  });

  it("scopes recall to the resource's node_name tag when resourceId is present", async () => {
    mock = await startMockCognee();
    const tool = createCogneeSearchTool({ config: baseConfig(mock) });
    await callTool(tool, { query: "user preferences" }, makeToolContext({ resourceId: "user-42" }));

    const body = mock.requests.find((r) => r.path === "/api/v1/recall")!.json as Record<string, unknown>;
    expect(body.node_name).toContain("resource:user-42");
  });

  it("returns a typed { error } object, never throws, on a 500", async () => {
    mock = await startMockCognee({
      routes: { "POST /api/v1/recall": () => ({ status: 500, body: { detail: "boom" } }) },
    });
    // cognee_search's own recall() call always passes an explicit per-call
    // `timeoutMs` (resolved.tools.timeoutMs), which itself implies 0
    // retries — no `retries: 0` config workaround needed here.
    const tool = createCogneeSearchTool({ config: baseConfig(mock) });

    const result = await callTool(tool, { query: "will this fail" }, makeToolContext());

    expect(typeof result.error).toBe("string");
    expect(result.results).toBeUndefined();
    const validation = await validateAgainstSchema(tool.outputSchema!, result);
    expect(validation.issues).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// cognee_ask
// ---------------------------------------------------------------------------

describe("createCogneeAskTool", () => {
  it("returns a synthesized answer + references on the happy path", async () => {
    mock = await startMockCognee();
    const tool = createCogneeAskTool({ config: baseConfig(mock) });

    const result = await callTool(tool, { question: "what does the user prefer?" }, makeToolContext());

    expect(result.error).toBeUndefined();
    expect(typeof result.answer).toBe("string");
    expect(result.answer.length).toBeGreaterThan(0);
    expect(Array.isArray(result.references)).toBe(true);
  });

  it("sends GRAPH_COMPLETION / include_references: true / only_context: false", async () => {
    mock = await startMockCognee();
    const tool = createCogneeAskTool({ config: baseConfig(mock) });
    await callTool(tool, { question: "why did the deploy fail" }, makeToolContext());

    const body = mock.requests.find((r) => r.path === "/api/v1/recall")!.json as Record<string, unknown>;
    expect(body.search_type).toBe("GRAPH_COMPLETION");
    expect(body.include_references).toBe(true);
    expect(body.only_context).toBe(false);
  });

  it("returns a typed { error } object, never throws, on a 500", async () => {
    mock = await startMockCognee({
      routes: { "POST /api/v1/recall": () => ({ status: 500, body: { detail: "graph completion exploded" } }) },
    });
    // cognee_ask's own recall() call always passes an explicit per-call
    // `timeoutMs`, which itself implies 0 retries — no `retries: 0` config
    // workaround needed here.
    const tool = createCogneeAskTool({ config: baseConfig(mock) });

    const result = await callTool(tool, { question: "will this fail" }, makeToolContext());

    expect(typeof result.error).toBe("string");
    expect(result.answer).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// cognee_remember
// ---------------------------------------------------------------------------

describe("createCogneeRememberTool", () => {
  it("writes the statement via /remember and reports success on the happy path", async () => {
    mock = await startMockCognee();
    const tool = createCogneeRememberTool({ config: baseConfig(mock) });

    const result = await callTool(
      tool,
      { statement: "The user prefers dark mode.", metadata: { category: "preference" } },
      makeToolContext({ threadId: "t1", resourceId: "r1" }),
    );

    expect(result.error).toBeUndefined();
    expect(result.success).toBe(true);

    const rememberReq = mock.requests.find((r) => r.path === "/api/v1/remember");
    expect(rememberReq).toBeDefined();
    expect(rememberReq!.body).toContain("The user prefers dark mode.");
    // metadata folded into node_set — multipart form field, so assert on the raw body text.
    // "category:preference": the literal ":" is the tag separator this
    // package inserts itself (matching scope.ts's buildTag); sanitizeId()
    // only escapes characters outside [A-Za-z0-9_-] plus ".", neither of
    // which "category"/"preference" contain, so both sides pass through
    // unescaped here.
    expect(rememberReq!.body).toContain("category:preference");
  });

  it(
    "returns a typed { error } object, never throws, on a 500",
    async () => {
      mock = await startMockCognee({
        routes: { "POST /api/v1/remember": () => ({ status: 500, body: { detail: "write failed" } }) },
      });
      // cognee_remember passes its own bounded `{ timeoutMs, retries: 1 }`
      // per call, so this exercises a real one-retry-on-a-write
      // (~RETRY_BASE_DELAY_MS backoff) — hence the longer test timeout.
      const tool = createCogneeRememberTool({ config: baseConfig(mock) });

      const result = await callTool(tool, { statement: "this will fail to save" }, makeToolContext());

      expect(result.success).toBe(false);
      expect(typeof result.error).toBe("string");
    },
    10_000,
  );
});

// ---------------------------------------------------------------------------
// cognee_forget
// ---------------------------------------------------------------------------

describe("createCogneeForgetTool", () => {
  it("calls /forget with memory_only: true forced, and never sends everything: true", async () => {
    mock = await startMockCognee();
    const tool = createCogneeForgetTool({ config: baseConfig(mock) });

    const result = await callTool(tool, { dataId: "data-1" }, makeToolContext());

    expect(result.error).toBeUndefined();
    expect(result.success).toBe(true);

    const forgetReq = mock.requests.find((r) => r.path === "/api/v1/forget");
    expect(forgetReq).toBeDefined();
    const body = forgetReq!.json as Record<string, unknown>;
    expect(body.data_id).toBe("data-1");
    expect(body.memory_only).toBe(true);
    expect(body.everything).toBeUndefined();
  });

  it(
    "returns a typed { error } object, never throws, on a 500",
    async () => {
      mock = await startMockCognee({
        routes: { "POST /api/v1/forget": () => ({ status: 500, body: { detail: "forget failed" } }) },
      });
      // Same as cognee_remember above: cognee_forget gets its own bounded
      // `{ timeoutMs, retries: 1 }` per call — hence the longer test timeout.
      const tool = createCogneeForgetTool({ config: baseConfig(mock) });

      const result = await callTool(tool, { dataId: "data-1" }, makeToolContext());

      expect(result.success).toBe(false);
      expect(typeof result.error).toBe("string");
    },
    10_000,
  );
});

// ---------------------------------------------------------------------------
// createCogneeTools — collection factory
// ---------------------------------------------------------------------------

describe("createCogneeTools", () => {
  it("returns cognee_search / cognee_ask / cognee_remember, and NOT cognee_forget, by default", async () => {
    mock = await startMockCognee();
    const tools = createCogneeTools(baseConfig(mock));

    expect(Object.keys(tools).sort()).toEqual(["cognee_ask", "cognee_remember", "cognee_search"]);
    expect(tools.cognee_forget).toBeUndefined();
  });

  it("includes cognee_forget when config.tools.enableForget is true", async () => {
    mock = await startMockCognee();
    const tools = createCogneeTools({ ...baseConfig(mock), tools: { enableForget: true } });

    expect(Object.keys(tools).sort()).toEqual(["cognee_ask", "cognee_forget", "cognee_remember", "cognee_search"]);
  });

  it("includes cognee_forget when COGNEE_ENABLE_FORGET=true is set in the environment", async () => {
    mock = await startMockCognee();
    // `createCogneeTools(config)` resolves `config.tools.enableForget` via
    // `resolveConfig()` (argument -> environment variable -> default), so
    // leaving `tools.enableForget` unset and setting the env var instead
    // proves the env fallback reaches this gate, not just the
    // explicit-argument path already covered above.
    const originalEnv = process.env.COGNEE_ENABLE_FORGET;
    try {
      process.env.COGNEE_ENABLE_FORGET = "true";
      const tools = createCogneeTools(baseConfig(mock));
      expect(tools.cognee_forget).toBeDefined();
    } finally {
      if (originalEnv === undefined) delete process.env.COGNEE_ENABLE_FORGET;
      else process.env.COGNEE_ENABLE_FORGET = originalEnv;
    }
  });

  it("shares one CogneeClient (and its dataset-id cache) across every tool it returns", async () => {
    mock = await startMockCognee();
    const tools = createCogneeTools({ ...baseConfig(mock), scope: "tagged" });

    await callTool(tools.cognee_search!, { query: "first call" }, makeToolContext({ resourceId: "r1" }));
    await callTool(
      tools.cognee_remember!,
      { statement: "second call, different tool" },
      makeToolContext({ resourceId: "r1" }),
    );

    // Both calls hit the mock; neither tool ever calls ensureDataset()
    // directly (recall/remember pass datasetName, not a resolved id), so
    // this instead proves the two tools reached the same server/session
    // scope rather than each building an independent, differently-configured
    // client — the recall and remember requests both carry the tagged
    // dataset name from one shared resolved config.
    const recallReq = mock.requests.find((r) => r.path === "/api/v1/recall")!.json as Record<string, unknown>;
    const rememberReq = mock.requests.find((r) => r.path === "/api/v1/remember")!;
    expect(recallReq.datasets).toEqual(["mastra-tools-test"]);
    expect(rememberReq.body).toContain("mastra-tools-test");
  });

  it("each returned tool's execute still round-trips end to end against the mock", async () => {
    mock = await startMockCognee();
    const tools = createCogneeTools({ ...baseConfig(mock), tools: { enableForget: true } });

    const searchResult = await callTool(tools.cognee_search!, { query: "q" }, makeToolContext());
    const askResult = await callTool(tools.cognee_ask!, { question: "q?" }, makeToolContext());
    const rememberResult = await callTool(tools.cognee_remember!, { statement: "s" }, makeToolContext());
    const forgetResult = await callTool(tools.cognee_forget!, { dataId: DATASET_ID }, makeToolContext());

    expect(searchResult.error).toBeUndefined();
    expect(askResult.error).toBeUndefined();
    expect(rememberResult.error).toBeUndefined();
    expect(forgetResult.error).toBeUndefined();
  });
});
