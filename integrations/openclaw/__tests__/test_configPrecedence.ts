import assert from "node:assert/strict";
import {
  DEFAULT_AUTO_RECALL,
  DEFAULT_BASE_URL,
  DEFAULT_DATASET_NAME,
  DEFAULT_MAX_RESULTS,
  DEFAULT_MIN_SCORE,
  resolveConfig,
} from "../src/config";
import type { CogneePluginConfig } from "../src/types";

type MatrixRow = {
  configKey: keyof ReturnType<typeof resolveConfig>;
  envVar: string;
  fileValue: unknown;
  envValue: string;
  defaultValue: unknown;
  fileConfig?: CogneePluginConfig;
};

const PRECEDENCE_MATRIX: MatrixRow[] = [
  {
    configKey: "baseUrl",
    envVar: "COGNEE_BASE_URL",
    fileValue: "http://file:9000",
    envValue: "http://env:8000",
    defaultValue: DEFAULT_BASE_URL,
    fileConfig: { baseUrl: "http://file:9000" },
  },
  {
    configKey: "datasetName",
    envVar: "COGNEE_PLUGIN_DATASET",
    fileValue: "file-dataset",
    envValue: "env-dataset",
    defaultValue: DEFAULT_DATASET_NAME,
    fileConfig: { datasetName: "file-dataset" },
  },
  {
    configKey: "maxResults",
    envVar: "COGNEE_MAX_RESULTS",
    fileValue: 7,
    envValue: "9",
    defaultValue: DEFAULT_MAX_RESULTS,
    fileConfig: { maxResults: 7 },
  },
  {
    configKey: "minScore",
    envVar: "COGNEE_MIN_SCORE",
    fileValue: 0.5,
    envValue: "0.8",
    defaultValue: DEFAULT_MIN_SCORE,
    fileConfig: { minScore: 0.5 },
  },
  {
    configKey: "autoRecall",
    envVar: "COGNEE_AUTO_RECALL",
    fileValue: false,
    envValue: "true",
    defaultValue: DEFAULT_AUTO_RECALL,
    fileConfig: { autoRecall: false },
  },
  {
    configKey: "mode",
    envVar: "COGNEE_MODE",
    fileValue: "local",
    envValue: "cloud",
    defaultValue: "local",
    fileConfig: { mode: "local" },
  },
  {
    configKey: "username",
    envVar: "COGNEE_USERNAME",
    fileValue: "file-user",
    envValue: "env-user",
    defaultValue: "",
    fileConfig: { username: "file-user" },
  },
  {
    configKey: "agentId",
    envVar: "OPENCLAW_AGENT_ID",
    fileValue: "file-agent",
    envValue: "env-agent",
    defaultValue: "default",
    fileConfig: { agentId: "file-agent" },
  },
  {
    configKey: "searchType",
    envVar: "COGNEE_SEARCH_TYPE",
    fileValue: "CHUNKS",
    envValue: "RAG_COMPLETION",
    defaultValue: "GRAPH_COMPLETION",
    fileConfig: { searchType: "CHUNKS" },
  },
  {
    configKey: "deleteMode",
    envVar: "COGNEE_DELETE_MODE",
    fileValue: "soft",
    envValue: "hard",
    defaultValue: "soft",
    fileConfig: { deleteMode: "soft" },
  },
];

function clearEnv(keys: string[]) {
  for (const key of keys) {
    delete process.env[key];
  }
}

function expectEnvValue(
  configKey: keyof ReturnType<typeof resolveConfig>,
  envValue: string,
  defaultValue: unknown,
  actual: ReturnType<typeof resolveConfig>,
) {
  if (typeof defaultValue === "number") {
    assert.equal(actual[configKey], Number(envValue));
    return;
  }
  if (typeof defaultValue === "boolean") {
    assert.equal(actual[configKey], envValue === "true" || envValue === "1");
    return;
  }
  if (configKey === "mode") {
    assert.equal(actual.mode, envValue);
    return;
  }
  assert.equal(actual[configKey], envValue);
}

export function runConfigPrecedenceTests(): void {
  const envKeys = PRECEDENCE_MATRIX.map((row) => row.envVar);

  for (const row of PRECEDENCE_MATRIX) {
    clearEnv(envKeys);

    const defaultsOnly = resolveConfig({});
    assert.equal(defaultsOnly[row.configKey], row.defaultValue);

    const fromFile = resolveConfig(row.fileConfig ?? {});
    assert.equal(
      fromFile[row.configKey],
      row.fileConfig?.[row.configKey as keyof CogneePluginConfig] ?? row.defaultValue,
    );

    process.env[row.envVar] = row.envValue;
    const fromEnv = resolveConfig(row.fileConfig ?? {});
    expectEnvValue(row.configKey, row.envValue, row.defaultValue, fromEnv);
    clearEnv(envKeys);
  }

  process.env.COGNEE_API_KEY = "env-key";
  assert.equal(resolveConfig({ apiKey: "file-key" }).apiKey, "env-key");
  delete process.env.COGNEE_API_KEY;
}

function isDirectRun(): boolean {
  const entry = process.argv[1]?.replace(/\\/g, "/") ?? "";
  return entry.endsWith("test_configPrecedence.ts");
}

if (isDirectRun()) {
  runConfigPrecedenceTests();
  console.log("PASS config precedence matrix");
}

const jestDescribe = (globalThis as { describe?: typeof describe }).describe;
if (jestDescribe) {
  jestDescribe("resolveConfig precedence (env > plugin config > defaults)", () => {
  const envKeys = PRECEDENCE_MATRIX.map((row) => row.envVar);

  afterEach(() => {
    clearEnv(envKeys);
    delete process.env.COGNEE_API_KEY;
  });

  it.each(PRECEDENCE_MATRIX)(
    "$configKey uses default, then file, then env",
    ({ configKey, envVar, envValue, defaultValue, fileConfig }) => {
      clearEnv(envKeys);

      const defaultsOnly = resolveConfig({});
      expect(defaultsOnly[configKey]).toEqual(defaultValue);

      const fromFile = resolveConfig(fileConfig ?? {});
      expect(fromFile[configKey]).toEqual(
        fileConfig?.[configKey as keyof CogneePluginConfig] ?? defaultValue,
      );

      process.env[envVar] = envValue;
      const fromEnv = resolveConfig(fileConfig ?? {});
      if (typeof defaultValue === "number") {
        expect(fromEnv[configKey]).toEqual(Number(envValue));
      } else if (typeof defaultValue === "boolean") {
        expect(fromEnv[configKey]).toBe(envValue === "true" || envValue === "1");
      } else if (configKey === "mode") {
        expect(fromEnv.mode).toBe(envValue);
      } else {
        expect(fromEnv[configKey]).toBe(envValue);
      }
    },
  );

  it("COGNEE_API_KEY overrides plugin apiKey", () => {
    process.env.COGNEE_API_KEY = "env-key";
    const cfg = resolveConfig({ apiKey: "file-key" });
    expect(cfg.apiKey).toBe("env-key");
  });
  });
}
