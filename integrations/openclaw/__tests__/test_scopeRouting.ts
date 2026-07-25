import { DEFAULT_SCOPE_ROUTING } from "../src/config";
import {
  cogneeSessionId,
  datasetNameForScope,
  matchGlob,
  routeFileToScope,
} from "../src/scope";
import type { CogneePluginConfig, MemoryScope, ScopeRoute } from "../src/types";

function baseCfg(overrides: Partial<CogneePluginConfig> = {}): Required<CogneePluginConfig> {
  return {
    mode: "local",
    baseUrl: "http://test",
    apiKey: "key",
    username: "",
    password: "",
    datasetName: "openclaw",
    companyDataset: "",
    userDatasetPrefix: "",
    agentDatasetPrefix: "",
    agentDatasetTemplate: "",
    userId: "",
    agentId: "default",
    recallScopes: ["agent", "user", "company"] as MemoryScope[],
    defaultWriteScope: "agent",
    scopeRouting: DEFAULT_SCOPE_ROUTING,
    enableSessions: true,
    persistSessionsAfterEnd: true,
    searchPrompt: "",
    searchType: "GRAPH_COMPLETION",
    deleteMode: "soft",
    maxResults: 3,
    minScore: 0.3,
    maxTokens: 512,
    autoRecall: true,
    autoIndex: true,
    autoCognify: true,
    autoMemify: false,
    improveOnSessionEnd: true,
    requestTimeoutMs: 60_000,
    ingestionTimeoutMs: 300_000,
    ...overrides,
  } as Required<CogneePluginConfig>;
}

describe("matchGlob edge cases", () => {
  it.each([
    ["exact file", "MEMORY.md", "MEMORY.md", true],
    ["exact file is case-sensitive", "MEMORY.md", "memory.md", false],
    ["single star stays within one path segment", "memory/*", "memory/tools.md", true],
    ["single star does not cross path separators", "memory/*", "memory/deep/tools.md", false],
    ["double star matches nested paths", "memory/company/**", "memory/company/a/b.md", true],
    ["double star with slash matches zero nested directories", "memory/**/policy.md", "memory/policy.md", true],
    ["question mark matches one non-separator char", "memory/?.md", "memory/a.md", true],
    ["question mark does not match two chars", "memory/?.md", "memory/ab.md", false],
    ["character class matches listed chars", "memory/[abc].md", "memory/b.md", true],
    ["negated character class rejects listed chars", "memory/[!abc].md", "memory/a.md", false],
    ["negated character class accepts other chars", "memory/[!abc].md", "memory/d.md", true],
    ["windows paths are normalized", "memory/company/**", "memory\\company\\policy.md", true],
    ["trailing double star requires the directory prefix", "memory/**", "memory", false],
  ])("%s", (_name, pattern, filePath, expected) => {
    expect(matchGlob(pattern, filePath)).toBe(expected);
  });
});

describe("routeFileToScope", () => {
  it.each([
    ["company nested file", "memory/company/policy.md", "company"],
    ["company deeper file", "memory/company/process/security.md", "company"],
    ["user nested file", "memory/user/preferences.md", "user"],
    ["user deeper file", "memory/user/history/item.md", "user"],
    ["agent memory file", "memory/tools.md", "agent"],
    ["root MEMORY.md", "MEMORY.md", "agent"],
  ] as Array<[string, string, MemoryScope]>)("routes %s", (_name, filePath, expectedScope) => {
    expect(routeFileToScope(filePath, DEFAULT_SCOPE_ROUTING, "agent")).toBe(expectedScope);
  });

  it("falls back to defaultWriteScope when no route matches", () => {
    expect(routeFileToScope("notes/private.md", DEFAULT_SCOPE_ROUTING, "user")).toBe("user");
  });

  it("uses first matching route instead of most-specific route", () => {
    const routes: ScopeRoute[] = [
      { pattern: "memory/**", scope: "agent" },
      { pattern: "memory/company/**", scope: "company" },
    ];

    expect(routeFileToScope("memory/company/policy.md", routes, "user")).toBe("agent");
  });

  it("normalizes Windows paths before routing", () => {
    expect(routeFileToScope("memory\\user\\preferences.md", DEFAULT_SCOPE_ROUTING, "agent")).toBe(
      "user",
    );
  });
});

describe("datasetNameForScope", () => {
  it("uses explicit companyDataset before fallback naming", () => {
    expect(datasetNameForScope("company", baseCfg({ companyDataset: "acme-shared" }))).toBe(
      "acme-shared",
    );
  });

  it("falls back to datasetName-company for company scope", () => {
    expect(datasetNameForScope("company", baseCfg({ datasetName: "workspace" }))).toBe(
      "workspace-company",
    );
  });

  it("uses userDatasetPrefix and userId for user scope", () => {
    expect(
      datasetNameForScope("user", baseCfg({ userDatasetPrefix: "workspace-user", userId: "alice" })),
    ).toBe("workspace-user-alice");
  });

  it("falls back to datasetName-user-default without userId", () => {
    expect(datasetNameForScope("user", baseCfg({ datasetName: "workspace" }))).toBe(
      "workspace-user-default",
    );
  });

  it("uses runtime agent id before config agent id for agent scope", () => {
    expect(
      datasetNameForScope(
        "agent",
        baseCfg({ agentDatasetPrefix: "workspace-agent", agentId: "static" }),
        "runtime",
      ),
    ).toBe("workspace-agent-runtime");
  });

  it("falls back to config agent id when runtime agent id is empty", () => {
    expect(
      datasetNameForScope(
        "agent",
        baseCfg({ agentDatasetPrefix: "workspace-agent", agentId: "static" }),
        "   ",
      ),
    ).toBe("workspace-agent-static");
  });

  it("uses agentDatasetTemplate before agentDatasetPrefix", () => {
    expect(
      datasetNameForScope(
        "agent",
        baseCfg({
          agentDatasetPrefix: "ignored",
          agentDatasetTemplate: "memory-{agentId}-prod",
          agentId: "static",
        }),
        "coder",
      ),
    ).toBe("memory-coder-prod");
  });

  it("lowercases agent id only when perAgentMemory is enabled", () => {
    const cfg = baseCfg({
      agentDatasetPrefix: "workspace-agent",
      agentId: "StaticAgent",
      perAgentMemory: true,
    });

    expect(datasetNameForScope("agent", cfg, "RuntimeAgent")).toBe(
      "workspace-agent-runtimeagent",
    );
  });

  it("preserves agent id case when perAgentMemory is disabled", () => {
    const cfg = baseCfg({
      agentDatasetPrefix: "workspace-agent",
      agentId: "StaticAgent",
      perAgentMemory: false,
    });

    expect(datasetNameForScope("agent", cfg, "RuntimeAgent")).toBe(
      "workspace-agent-RuntimeAgent",
    );
  });
});

describe("cogneeSessionId", () => {
  it.each([
    ["undefined input", undefined, ""],
    ["empty string", "", ""],
    ["whitespace only", "   ", ""],
    ["all invalid chars", "####", ""],
    ["valid id", "session-123", "open_claw_session-123"],
    ["spaces and symbols become underscores", "session/12 3!", "open_claw_session_12_3"],
    ["path traversal-like input is neutralized", "../../etc/passwd", "open_claw_etc_passwd"],
    ["leading and trailing dots are stripped", "...abc...", "open_claw_abc"],
    ["leading and trailing underscores are stripped", "__abc__", "open_claw_abc"],
    ["allowed punctuation is preserved inside", "a-b_c.d", "open_claw_a-b_c.d"],
    ["non-ascii letters are replaced then trimmed", "caf\u00e9", "open_claw_caf"],
    ["emoji is replaced once", "a\u{1F600}b", "open_claw_a_b"],
    ["already-prefixed ids are characterized", "open_claw_x", "open_claw_open_claw_x"],
  ] as Array<[string, string | undefined, string]>)("%s", (_name, input, expected) => {
    expect(cogneeSessionId(input)).toBe(expected);
  });

  it("caps the native session id at 120 characters before prefixing", () => {
    const result = cogneeSessionId("a".repeat(200));

    expect(result).toBe(`open_claw_${"a".repeat(120)}`);
    expect(result.length).toBe("open_claw_".length + 120);
  });
});
