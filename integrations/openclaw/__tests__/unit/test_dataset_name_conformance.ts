import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { defaultCodeDataset } from "../../src/code-graph";
import { resolveConfig } from "../../src/config";
import { datasetNameForScope, sanitizeDatasetName } from "../../src/scope";

type SanitizeCase = { input: string; fallback: string; expected: string; note: string };

// The shared case table lives at integrations/conformance/dataset_name_cases.json.
// The claude-code, codex, antigravity and hermes-agent tests read the same file,
// so any implementation that drifts from the shared rule fails its test.
const casesPath = resolve(__dirname, "..", "..", "..", "conformance", "dataset_name_cases.json");
const cases: SanitizeCase[] = JSON.parse(readFileSync(casesPath, "utf-8"));

describe("dataset-name sanitization conformance", () => {
  it.each(cases)("$note", ({ input, fallback, expected }) => {
    expect(sanitizeDatasetName(input, fallback)).toBe(expected);
  });
});

describe("dataset names reach cognee sanitized", () => {
  const saved = process.env.COGNEE_PLUGIN_DATASET;
  afterEach(() => {
    if (saved === undefined) delete process.env.COGNEE_PLUGIN_DATASET;
    else process.env.COGNEE_PLUGIN_DATASET = saved;
  });

  it("sanitizes the configured dataset name", () => {
    process.env.COGNEE_PLUGIN_DATASET = "my project.v2";
    expect(resolveConfig({}).datasetName).toBe("my_project_v2");
  });

  it("leaves a name the server accepts unchanged", () => {
    process.env.COGNEE_PLUGIN_DATASET = "Foo+Bar";
    expect(resolveConfig({}).datasetName).toBe("Foo+Bar");
  });

  it("sanitizes names derived from user and agent ids", () => {
    delete process.env.COGNEE_PLUGIN_DATASET;
    const cfg = resolveConfig({ datasetName: "team", userId: "a.b@x.com" });
    expect(datasetNameForScope("user", cfg)).toBe("team-user-a_b@x_com");
  });

  it("gives code-graph datasets a dot-free name", () => {
    const name = defaultCodeDataset("/work/Foo.JS");
    expect(name.startsWith("codebase-foo-js-")).toBe(true);
    expect(name).not.toContain(".");
  });
});
