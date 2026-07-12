/**
 * Config precedence tests for the Cognee openclaw plugin (#3559).
 *
 * Documents the ACTUAL current behavior of resolveConfig(): the plugin config
 * object (raw) is checked before process.env, so the **config file wins** over
 * env for most settings. Two settings are asymmetric and are NOT flat file-wins:
 * `mode` (env can only force "cloud") and `apiKey` (env consulted only in cloud
 * mode; config values support ${VAR} interpolation). This file only pins that
 * behavior down; it changes no runtime logic.
 */

import { resolveConfig, resolveEnvVars } from "../src/config";

// Every key any test touches -- snapshotted and cleared before each test, then
// restored after, so tests never leak env into one another (or the runner).
const TOUCHED_KEYS = [
  "COGNEE_BASE_URL",
  "COGNEE_MODE",
  "COGNEE_API_KEY",
  "COGNEE_DATASET_NAME",
  "MY_KEY",
];

let savedEnv: Record<string, string | undefined>;

beforeEach(() => {
  savedEnv = {};
  for (const key of TOUCHED_KEYS) {
    savedEnv[key] = process.env[key];
    delete process.env[key];
  }
});

afterEach(() => {
  for (const key of TOUCHED_KEYS) {
    if (savedEnv[key] === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = savedEnv[key];
    }
  }
});

describe("resolveConfig precedence (file-wins)", () => {
  test("config overrides env", () => {
    process.env.COGNEE_BASE_URL = "http://from-env";
    expect(resolveConfig({ baseUrl: "http://from-config" }).baseUrl).toBe("http://from-config");
  });

  test("env used when config empty", () => {
    process.env.COGNEE_BASE_URL = "http://from-env";
    expect(resolveConfig({}).baseUrl).toBe("http://from-env");
  });

  test("default when neither", () => {
    expect(resolveConfig({}).baseUrl).toBe("http://localhost:8000");
  });

  test("whitespace-only config value trims to falsy and falls through", () => {
    // No env set -> trimmed "" is falsy -> falls through to the default.
    expect(resolveConfig({ baseUrl: "   " }).baseUrl).toBe("http://localhost:8000");
  });

  test("config-only field ignores env", () => {
    // datasetName has no env read, so a bogus env var cannot affect it.
    process.env.COGNEE_DATASET_NAME = "bogus";
    expect(resolveConfig({}).datasetName).toBe("openclaw");
  });
});

describe("resolveConfig mode asymmetry (env can only force cloud)", () => {
  test("env forces cloud over config local", () => {
    process.env.COGNEE_MODE = "cloud";
    expect(resolveConfig({ mode: "local" }).mode).toBe("cloud");
  });

  test("config cloud with no env", () => {
    expect(resolveConfig({ mode: "cloud" }).mode).toBe("cloud");
  });

  test("env cannot force local", () => {
    process.env.COGNEE_MODE = "local";
    expect(resolveConfig({ mode: "cloud" }).mode).toBe("cloud");
  });
});

describe("resolveConfig apiKey asymmetry", () => {
  test("cloud mode reads COGNEE_API_KEY from env", () => {
    process.env.COGNEE_API_KEY = "k";
    expect(resolveConfig({ mode: "cloud" }).apiKey).toBe("k");
  });

  test("local mode ignores COGNEE_API_KEY from env", () => {
    process.env.COGNEE_API_KEY = "k";
    expect(resolveConfig({ mode: "local" }).apiKey).toBe("");
  });

  test("${VAR} interpolation resolves from env", () => {
    process.env.MY_KEY = "secret";
    expect(resolveConfig({ apiKey: "${MY_KEY}", mode: "cloud" }).apiKey).toBe("secret");
  });

  test("${VAR} with a missing env var throws", () => {
    expect(() => resolveConfig({ apiKey: "${MY_KEY}", mode: "cloud" })).toThrow();
    expect(() => resolveEnvVars("${MY_KEY}")).toThrow();
  });
});
