/**
 * No network, no real `process.env` mutation: every case passes its own
 * `env` object as the second argument (see `config.ts`'s header comment).
 */

import { describe, expect, it, jest } from "@jest/globals";
import {
  DEFAULT_BASE_URL,
  DEFAULT_DATASET,
  DEFAULT_DATASET_PREFIX,
  DEFAULT_ENABLE_FORGET,
  DEFAULT_INCLUDE_REFERENCES,
  DEFAULT_MIN_QUERY_LENGTH,
  DEFAULT_RECALL_BUDGET_MS,
  DEFAULT_RECALL_ENABLED,
  DEFAULT_RECALL_TIMEOUT_MS,
  DEFAULT_REQUEST_TIMEOUT_MS,
  DEFAULT_RETRIES,
  DEFAULT_RUN_IN_BACKGROUND,
  DEFAULT_SCOPE,
  DEFAULT_SEARCH_TYPE,
  DEFAULT_TOP_K,
  DEFAULT_TOOLS_TIMEOUT_MS,
  DEFAULT_WRITE_MAX_CHARS,
  DEFAULT_WRITE_MODE,
  redactConfigForLogging,
  resolveConfig,
} from "../../src/config.js";

/** A clean env object with no COGNEE_* keys, so a test only sees the vars it sets. */
function emptyEnv(overrides: NodeJS.ProcessEnv = {}): NodeJS.ProcessEnv {
  return { ...overrides };
}

describe("resolveConfig — defaults", () => {
  it("fills every field with its default when nothing is supplied", () => {
    const resolved = resolveConfig({}, emptyEnv());

    expect(resolved.baseUrl).toBe(DEFAULT_BASE_URL);
    expect(resolved.apiKey).toBeUndefined();
    expect(resolved.auth).toBeUndefined();
    expect(resolved.dataset).toBe(DEFAULT_DATASET);
    expect(resolved.datasetPrefix).toBe(DEFAULT_DATASET_PREFIX);
    expect(resolved.scope).toBe(DEFAULT_SCOPE);
    expect(resolved.nodeSet).toEqual([]);

    expect(resolved.recall).toEqual({
      enabled: DEFAULT_RECALL_ENABLED,
      searchType: DEFAULT_SEARCH_TYPE,
      topK: DEFAULT_TOP_K,
      minQueryLength: DEFAULT_MIN_QUERY_LENGTH,
      timeoutMs: DEFAULT_RECALL_TIMEOUT_MS,
      budgetMs: DEFAULT_RECALL_BUDGET_MS,
      includeReferences: DEFAULT_INCLUDE_REFERENCES,
    });

    expect(resolved.write).toEqual({
      mode: DEFAULT_WRITE_MODE,
      runInBackground: DEFAULT_RUN_IN_BACKGROUND,
      maxChars: DEFAULT_WRITE_MAX_CHARS,
    });

    expect(resolved.tools).toEqual({ enableForget: DEFAULT_ENABLE_FORGET, timeoutMs: DEFAULT_TOOLS_TIMEOUT_MS });

    expect(resolved.requestTimeoutMs).toBe(DEFAULT_REQUEST_TIMEOUT_MS);
    expect(resolved.retries).toBe(DEFAULT_RETRIES);
    expect(resolved.debug).toBe(false);
    expect(resolved.fetch).toBeUndefined();
  });

  it("never throws when no credential is supplied at all", () => {
    expect(() => resolveConfig({}, emptyEnv())).not.toThrow();
    expect(() => resolveConfig(undefined, emptyEnv())).not.toThrow();
  });

  it("defaults nodeSet to a fresh empty array each call, not a shared reference", () => {
    const a = resolveConfig({}, emptyEnv());
    const b = resolveConfig({}, emptyEnv());
    expect(a.nodeSet).not.toBe(b.nodeSet);
  });
});

describe("resolveConfig — precedence: explicit > env > default", () => {
  it("baseUrl / COGNEE_API_URL", () => {
    expect(resolveConfig({}, emptyEnv()).baseUrl).toBe(DEFAULT_BASE_URL);
    expect(resolveConfig({}, emptyEnv({ COGNEE_API_URL: "http://env:9000" })).baseUrl).toBe("http://env:9000");
    expect(
      resolveConfig({ baseUrl: "http://explicit:9000" }, emptyEnv({ COGNEE_API_URL: "http://env:9000" })).baseUrl,
    ).toBe("http://explicit:9000");
  });

  it("apiKey / COGNEE_API_KEY", () => {
    expect(resolveConfig({}, emptyEnv()).apiKey).toBeUndefined();
    expect(resolveConfig({}, emptyEnv({ COGNEE_API_KEY: "env-key" })).apiKey).toBe("env-key");
    expect(resolveConfig({ apiKey: "explicit-key" }, emptyEnv({ COGNEE_API_KEY: "env-key" })).apiKey).toBe(
      "explicit-key",
    );
  });

  it("auth.email / COGNEE_USER_EMAIL and auth.password / COGNEE_USER_PASSWORD, independently", () => {
    const envOnly = resolveConfig({}, emptyEnv({ COGNEE_USER_EMAIL: "e@env", COGNEE_USER_PASSWORD: "p-env" }));
    expect(envOnly.auth).toEqual({ email: "e@env", password: "p-env" });

    const explicitWins = resolveConfig(
      { auth: { email: "e@explicit" } },
      emptyEnv({ COGNEE_USER_EMAIL: "e@env", COGNEE_USER_PASSWORD: "p-env" }),
    );
    expect(explicitWins.auth).toEqual({ email: "e@explicit", password: "p-env" });
  });

  it("both apiKey and auth can be resolved simultaneously — precedence between them is a client-side concern, not resolveConfig's to erase", () => {
    const resolved = resolveConfig(
      { apiKey: "explicit-key", auth: { email: "e@x", password: "p" } },
      emptyEnv(),
    );
    expect(resolved.apiKey).toBe("explicit-key");
    expect(resolved.auth).toEqual({ email: "e@x", password: "p" });
  });

  it("dataset / COGNEE_DATASET", () => {
    expect(resolveConfig({}, emptyEnv({ COGNEE_DATASET: "env-ds" })).dataset).toBe("env-ds");
    expect(resolveConfig({ dataset: "explicit-ds" }, emptyEnv({ COGNEE_DATASET: "env-ds" })).dataset).toBe(
      "explicit-ds",
    );
  });

  it("datasetPrefix / COGNEE_DATASET_PREFIX", () => {
    expect(resolveConfig({}, emptyEnv({ COGNEE_DATASET_PREFIX: "env_" })).datasetPrefix).toBe("env_");
    expect(
      resolveConfig({ datasetPrefix: "explicit_" }, emptyEnv({ COGNEE_DATASET_PREFIX: "env_" })).datasetPrefix,
    ).toBe("explicit_");
  });

  it("scope / COGNEE_SCOPE, with an invalid value falling back to default", () => {
    expect(resolveConfig({}, emptyEnv({ COGNEE_SCOPE: "dataset-per-resource" })).scope).toBe(
      "dataset-per-resource",
    );
    expect(
      resolveConfig({ scope: "dataset-per-resource" }, emptyEnv({ COGNEE_SCOPE: "tagged" })).scope,
    ).toBe("dataset-per-resource");
    expect(resolveConfig({}, emptyEnv({ COGNEE_SCOPE: "bogus" })).scope).toBe(DEFAULT_SCOPE);
  });

  it("nodeSet is explicit-only (no env equivalent) and is copied, not aliased", () => {
    const tags = ["extra:tag"];
    const resolved = resolveConfig({ nodeSet: tags }, emptyEnv());
    expect(resolved.nodeSet).toEqual(["extra:tag"]);
    expect(resolved.nodeSet).not.toBe(tags);
  });

  it("recall.enabled / COGNEE_RECALL_ENABLED (boolean parsing)", () => {
    expect(resolveConfig({}, emptyEnv({ COGNEE_RECALL_ENABLED: "false" })).recall.enabled).toBe(false);
    expect(resolveConfig({}, emptyEnv({ COGNEE_RECALL_ENABLED: "0" })).recall.enabled).toBe(false);
    expect(resolveConfig({}, emptyEnv({ COGNEE_RECALL_ENABLED: "true" })).recall.enabled).toBe(true);
    expect(
      resolveConfig({ recall: { enabled: true } }, emptyEnv({ COGNEE_RECALL_ENABLED: "false" })).recall.enabled,
    ).toBe(true);
  });

  it("recall.searchType / COGNEE_SEARCH_TYPE passes through verbatim, no validation", () => {
    expect(resolveConfig({}, emptyEnv({ COGNEE_SEARCH_TYPE: "GRAPH_COMPLETION" })).recall.searchType).toBe(
      "GRAPH_COMPLETION",
    );
    expect(
      resolveConfig({ recall: { searchType: "TEMPORAL" } }, emptyEnv({ COGNEE_SEARCH_TYPE: "GRAPH_COMPLETION" }))
        .recall.searchType,
    ).toBe("TEMPORAL");
  });

  it("recall.topK / COGNEE_TOP_K (int parsing)", () => {
    expect(resolveConfig({}, emptyEnv({ COGNEE_TOP_K: "25" })).recall.topK).toBe(25);
    expect(resolveConfig({ recall: { topK: 3 } }, emptyEnv({ COGNEE_TOP_K: "25" })).recall.topK).toBe(3);
    // malformed env value degrades to default rather than NaN
    expect(resolveConfig({}, emptyEnv({ COGNEE_TOP_K: "not-a-number" })).recall.topK).toBe(DEFAULT_TOP_K);
  });

  it("recall.timeoutMs / COGNEE_RECALL_TIMEOUT_MS and recall.budgetMs / COGNEE_RECALL_BUDGET_MS", () => {
    const resolved = resolveConfig(
      {},
      emptyEnv({ COGNEE_RECALL_TIMEOUT_MS: "1000", COGNEE_RECALL_BUDGET_MS: "2000" }),
    );
    expect(resolved.recall.timeoutMs).toBe(1000);
    expect(resolved.recall.budgetMs).toBe(2000);
  });

  it("recall.minQueryLength and recall.includeReferences have no env var — explicit or default only", () => {
    expect(resolveConfig({}, emptyEnv()).recall.minQueryLength).toBe(DEFAULT_MIN_QUERY_LENGTH);
    expect(resolveConfig({ recall: { minQueryLength: 20 } }, emptyEnv()).recall.minQueryLength).toBe(20);
    expect(resolveConfig({}, emptyEnv()).recall.includeReferences).toBe(true);
    expect(resolveConfig({ recall: { includeReferences: false } }, emptyEnv()).recall.includeReferences).toBe(
      false,
    );
  });

  it("write.mode / COGNEE_SAVE_MODE, with an invalid value falling back to default", () => {
    expect(resolveConfig({}, emptyEnv({ COGNEE_SAVE_MODE: "assistant-only" })).write.mode).toBe(
      "assistant-only",
    );
    expect(
      resolveConfig({ write: { mode: "never" } }, emptyEnv({ COGNEE_SAVE_MODE: "always" })).write.mode,
    ).toBe("never");
    expect(resolveConfig({}, emptyEnv({ COGNEE_SAVE_MODE: "bogus" })).write.mode).toBe(DEFAULT_WRITE_MODE);
  });

  it("write.runInBackground and write.maxChars have no env var — explicit or default only", () => {
    expect(resolveConfig({}, emptyEnv()).write.runInBackground).toBe(true);
    expect(resolveConfig({ write: { runInBackground: false } }, emptyEnv()).write.runInBackground).toBe(false);
    expect(resolveConfig({}, emptyEnv()).write.maxChars).toBe(DEFAULT_WRITE_MAX_CHARS);
    expect(resolveConfig({ write: { maxChars: 500 } }, emptyEnv()).write.maxChars).toBe(500);
  });

  it("tools.enableForget / COGNEE_ENABLE_FORGET (boolean parsing)", () => {
    expect(resolveConfig({}, emptyEnv({ COGNEE_ENABLE_FORGET: "true" })).tools.enableForget).toBe(true);
    expect(
      resolveConfig({ tools: { enableForget: false } }, emptyEnv({ COGNEE_ENABLE_FORGET: "true" })).tools
        .enableForget,
    ).toBe(false);
    expect(resolveConfig({}, emptyEnv()).tools.enableForget).toBe(false);
  });

  it("tools.timeoutMs / COGNEE_TOOLS_TIMEOUT_MS", () => {
    expect(resolveConfig({}, emptyEnv()).tools.timeoutMs).toBe(DEFAULT_TOOLS_TIMEOUT_MS);
    expect(resolveConfig({}, emptyEnv({ COGNEE_TOOLS_TIMEOUT_MS: "5000" })).tools.timeoutMs).toBe(5000);
    expect(
      resolveConfig({ tools: { timeoutMs: 1234 } }, emptyEnv({ COGNEE_TOOLS_TIMEOUT_MS: "5000" })).tools.timeoutMs,
    ).toBe(1234);
  });

  it("requestTimeoutMs / COGNEE_TIMEOUT_MS", () => {
    expect(resolveConfig({}, emptyEnv({ COGNEE_TIMEOUT_MS: "60000" })).requestTimeoutMs).toBe(60000);
    expect(
      resolveConfig({ requestTimeoutMs: 1234 }, emptyEnv({ COGNEE_TIMEOUT_MS: "60000" })).requestTimeoutMs,
    ).toBe(1234);
  });

  it("retries / COGNEE_RETRIES", () => {
    expect(resolveConfig({}, emptyEnv({ COGNEE_RETRIES: "5" })).retries).toBe(5);
    expect(resolveConfig({ retries: 0 }, emptyEnv({ COGNEE_RETRIES: "5" })).retries).toBe(0);
  });

  it("debug / COGNEE_DEBUG (boolean parsing)", () => {
    const debugSpy = jest.spyOn(console, "debug").mockImplementation(() => undefined);
    try {
      expect(resolveConfig({}, emptyEnv({ COGNEE_DEBUG: "true" })).debug).toBe(true);
      expect(resolveConfig({ debug: false }, emptyEnv({ COGNEE_DEBUG: "true" })).debug).toBe(false);
    } finally {
      debugSpy.mockRestore();
    }
  });

  it("fetch is explicit-only — never read from env, never defaulted to a stand-in", () => {
    const injected = (() => Promise.resolve(new Response())) as unknown as typeof fetch;
    expect(resolveConfig({ fetch: injected }, emptyEnv()).fetch).toBe(injected);
    expect(resolveConfig({}, emptyEnv()).fetch).toBeUndefined();
  });

  it("honors explicit false/0/empty-array values rather than treating them as absent", () => {
    expect(resolveConfig({ recall: { enabled: false } }, emptyEnv()).recall.enabled).toBe(false);
    expect(resolveConfig({ retries: 0 }, emptyEnv()).retries).toBe(0);
    expect(resolveConfig({ nodeSet: [] }, emptyEnv({ COGNEE_DATASET: "x" })).nodeSet).toEqual([]);
  });
});

describe("resolveConfig — secret redaction", () => {
  it("never logs apiKey or auth.password even with debug: true", () => {
    const debugSpy = jest.spyOn(console, "debug").mockImplementation(() => undefined);
    try {
      resolveConfig(
        { debug: true, apiKey: "super-secret-key", auth: { email: "user@example.com", password: "super-secret-pw" } },
        emptyEnv(),
      );

      expect(debugSpy).toHaveBeenCalledTimes(1);
      const loggedArgs = debugSpy.mock.calls[0];
      const serialized = loggedArgs.map((arg) => (typeof arg === "string" ? arg : JSON.stringify(arg))).join(" ");

      expect(serialized).not.toContain("super-secret-key");
      expect(serialized).not.toContain("super-secret-pw");
      // The non-secret email is allowed through; only apiKey/password are redacted.
      expect(serialized).toContain("user@example.com");
    } finally {
      debugSpy.mockRestore();
    }
  });

  it("does not log at all when debug is false (the default)", () => {
    const debugSpy = jest.spyOn(console, "debug").mockImplementation(() => undefined);
    try {
      resolveConfig({ apiKey: "super-secret-key" }, emptyEnv());
      expect(debugSpy).not.toHaveBeenCalled();
    } finally {
      debugSpy.mockRestore();
    }
  });

  it("redactConfigForLogging masks apiKey and auth.password, drops fetch, and leaves everything else intact", () => {
    const injected = (() => Promise.resolve(new Response())) as unknown as typeof fetch;
    const resolved = resolveConfig(
      {
        apiKey: "super-secret-key",
        auth: { email: "user@example.com", password: "super-secret-pw" },
        dataset: "my-app",
        fetch: injected,
      },
      emptyEnv(),
    );

    const safe = redactConfigForLogging(resolved);
    const serialized = JSON.stringify(safe);

    expect(serialized).not.toContain("super-secret-key");
    expect(serialized).not.toContain("super-secret-pw");
    expect(safe.apiKey).toBe("***REDACTED***");
    expect((safe.auth as { email?: string; password?: string }).password).toBe("***REDACTED***");
    expect((safe.auth as { email?: string; password?: string }).email).toBe("user@example.com");
    expect(safe.dataset).toBe("my-app");
    expect("fetch" in safe).toBe(false);
  });

  it("redactConfigForLogging leaves apiKey/auth undefined when they were never set", () => {
    const resolved = resolveConfig({}, emptyEnv());
    const safe = redactConfigForLogging(resolved);
    expect(safe.apiKey).toBeUndefined();
    expect(safe.auth).toBeUndefined();
  });
});
