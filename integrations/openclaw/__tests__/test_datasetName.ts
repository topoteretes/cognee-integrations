/**
 * Regression tests for sanitizeDatasetName() and its integration into
 * datasetNameForScope() — issue #3549.
 *
 * Place this file at:
 *   integrations/openclaw/__tests__/test_datasetName.ts
 *
 * Run with the existing test runner (e.g. npx jest or npm test).
 */

import { sanitizeDatasetName, datasetNameForScope } from "../src/scope.js";
import type { CogneePluginConfig, MemoryScope } from "../src/types.js";

// ---------------------------------------------------------------------------
// Shared fixture — minimal Required<CogneePluginConfig>
// ---------------------------------------------------------------------------
function makeCfg(overrides: Partial<CogneePluginConfig> = {}): Required<CogneePluginConfig> {
  return {
    baseUrl: "http://localhost:8000",
    datasetName: "openclaw",
    searchType: "GRAPH_COMPLETION",
    minScore: 0.3,
    topK: 5,
    requestTimeout: 60_000,
    ingestionTimeout: 300_000,
    deleteMode: "soft",
    maxTokens: 512,
    autoRecall: true,
    autoRecallScope: ["agent", "user", "company"],
    scopeRouting: [],
    defaultScope: "agent" as MemoryScope,
    companyDataset: "",
    userDatasetPrefix: "",
    agentDatasetPrefix: "",
    agentDatasetTemplate: "",
    userId: "user1",
    agentId: "agent1",
    perAgentMemory: false,
    recallInjectionPosition: "system",
    persistSession: false,
    ...overrides,
  } as unknown as Required<CogneePluginConfig>;
}

// ---------------------------------------------------------------------------
// Issue #3549 canonical test cases — identical across all integrations
// ---------------------------------------------------------------------------
const CASES: [string, string, string][] = [
  // [input, fallback, expected]
  ["mydataset",    "fb", "mydataset"],
  ["project_01",   "fb", "project_01"],
  ["company-prod", "fb", "company-prod"],
  ["test.v1",      "fb", "test.v1"],
  ["My Dataset",   "fb", "My_Dataset"],
  ["My@Data",      "fb", "My_Data"],
  ["!!hello!!",    "fb", "hello"],
  ["###",          "fb", "fb"],
  ["........",     "fb", "fb"],
  ["___",          "fb", "fb"],
  ["a".repeat(200), "fb", "a".repeat(120)],
  ["你好 Dataset",  "fb", "Dataset"],
  ["",             "fb", "fb"],
  ["-leading",     "fb", "-leading"],
  [".leading",     "fb", "leading"],
  ["_leading",     "fb", "leading"],
  ["trailing.",    "fb", "trailing"],
];

describe("sanitizeDatasetName", () => {
  test("issue #3549 canonical cases", () => {
    for (const [input, fallback, expected] of CASES) {
      expect(sanitizeDatasetName(input, fallback)).toBe(expected);
    }
  });

  test("max length is 120", () => {
    expect(sanitizeDatasetName("x".repeat(200), "fb")).toHaveLength(120);
  });

  test("fallback fires only when result is empty after sanitization", () => {
    expect(sanitizeDatasetName("ok_name", "fb")).toBe("ok_name");
    expect(sanitizeDatasetName("###",     "fb")).toBe("fb");
  });

  test("null/undefined treated as empty string", () => {
    expect(sanitizeDatasetName(undefined as unknown as string, "fb")).toBe("fb");
    expect(sanitizeDatasetName(null as unknown as string, "fb")).toBe("fb");
  });
});

// ---------------------------------------------------------------------------
// datasetNameForScope — verifies sanitization is applied in every branch
// ---------------------------------------------------------------------------
describe("datasetNameForScope sanitizes composite names", () => {
  test("company scope: bad companyDataset is sanitised", () => {
    const cfg = makeCfg({ companyDataset: "My Company!" });
    expect(datasetNameForScope("company", cfg)).toBe("My_Company");
  });

  test("company scope: bad datasetName propagates and is sanitised", () => {
    const cfg = makeCfg({ datasetName: "my@claw", companyDataset: "" });
    // composite = "my@claw-company" → "my_claw-company"
    expect(datasetNameForScope("company", cfg)).toBe("my_claw-company");
  });

  test("user scope: userId with special chars is sanitised", () => {
    const cfg = makeCfg({ userId: "user@example.com", userDatasetPrefix: "" });
    // "openclaw-user-user@example.com" → "openclaw-user-user_example.com"
    expect(datasetNameForScope("user", cfg)).toBe("openclaw-user-user_example.com");
  });

  test("agent scope: agentDatasetTemplate with special chars is sanitised", () => {
    const cfg = makeCfg({ agentDatasetTemplate: "!!{agentId}!!", agentId: "bot" });
    // "!!bot!!" → "__bot__" → strip → "bot"
    expect(datasetNameForScope("agent", cfg)).toBe("bot");
  });

  test("agent scope: fully-invalid template falls back", () => {
    const cfg = makeCfg({ agentDatasetTemplate: "###", agentId: "bot" });
    expect(datasetNameForScope("agent", cfg)).toBe("openclaw-agent-default");
  });

  test("valid config produces unchanged dataset names", () => {
    const cfg = makeCfg({ datasetName: "openclaw", agentId: "bot" });
    expect(datasetNameForScope("company", cfg)).toBe("openclaw-company");
    expect(datasetNameForScope("user",    cfg)).toBe("openclaw-user-user1");
    expect(datasetNameForScope("agent",   cfg)).toBe("openclaw-agent-bot");
  });
});
