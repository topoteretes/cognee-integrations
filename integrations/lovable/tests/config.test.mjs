import test from "node:test";
import assert from "node:assert/strict";
import { resolveConfig, redactConfig, ConfigError } from "../dist/index.js";
import { BASE_ENV } from "./helpers.mjs";

test("explicit config beats environment beats defaults", () => {
  const config = resolveConfig({ projectDataset: "explicit_ds", limits: { defaultTopK: 3 } }, { ...BASE_ENV, LOVABLE_RECALL_TOP_K: "7" });
  assert.equal(config.projectDataset, "explicit_ds");
  assert.equal(config.limits.defaultTopK, 3);
  assert.equal(config.cognee.baseUrl, "http://cognee.test:8000");
  assert.equal(config.http.path, "/mcp");
  assert.equal(config.recall.defaultMode, "evidence");
});

test("required settings are reported together", () => {
  assert.throws(() => resolveConfig({}, {}), (error) => {
    assert.ok(error instanceof ConfigError);
    assert.match(error.message, /COGNEE_API_KEY/);
    assert.match(error.message, /LOVABLE_PROJECT_DATASET/);
    assert.match(error.message, /LOVABLE_MCP_TOKEN/);
    return true;
  });
});

test("tokens may be a comma-separated list for rotation and must be long enough", () => {
  const config = resolveConfig({}, { ...BASE_ENV, LOVABLE_MCP_TOKEN: "lvm_first_token_0123456789, lvm_second_token_0123456789" });
  assert.deepEqual(config.tokens, ["lvm_first_token_0123456789", "lvm_second_token_0123456789"]);
  assert.throws(() => resolveConfig({}, { ...BASE_ENV, LOVABLE_MCP_TOKEN: "short" }), /at least 16/);
});

test("shared datasets never include the project dataset and must be valid names", () => {
  const config = resolveConfig({}, { ...BASE_ENV, LOVABLE_SHARED_DATASETS: "lovable_probe, research, research" });
  assert.deepEqual(config.sharedDatasets, ["research"]);
  assert.throws(() => resolveConfig({}, { ...BASE_ENV, LOVABLE_SHARED_DATASETS: "bad name" }), /not a valid dataset name/);
  assert.throws(() => resolveConfig({}, { ...BASE_ENV, LOVABLE_PROJECT_DATASET: "../etc" }), /not a valid dataset name/);
});

test("allowed hosts derive from the public URL plus loopback; explicit list replaces them", () => {
  const derived = resolveConfig({}, { ...BASE_ENV, LOVABLE_MCP_PORT: "8787", LOVABLE_PUBLIC_URL: "https://memory.example.com/" });
  assert.ok(derived.http.allowedHosts.includes("memory.example.com"));
  assert.ok(derived.http.allowedHosts.includes("localhost:8787"));
  assert.deepEqual(derived.http.allowedOrigins, ["https://memory.example.com"]);
  assert.equal(derived.http.publicUrl, "https://memory.example.com");
  const explicit = resolveConfig({}, { ...BASE_ENV, LOVABLE_ALLOWED_HOSTS: "a.example.com:443", LOVABLE_PUBLIC_URL: "https://b.example.com" });
  assert.deepEqual(explicit.http.allowedHosts, ["a.example.com:443"]);
});

test("invalid numbers, booleans and modes are rejected", () => {
  assert.throws(() => resolveConfig({}, { ...BASE_ENV, LOVABLE_RECALL_TOP_K: "many" }), /non-negative integer/);
  assert.throws(() => resolveConfig({}, { ...BASE_ENV, LOVABLE_RECALL_ALLOW_ANSWER_MODE: "maybe" }), /boolean/);
  assert.throws(() => resolveConfig({}, { ...BASE_ENV, LOVABLE_RECALL_MODE: "magic" }), /LOVABLE_RECALL_MODE/);
  assert.throws(() => resolveConfig({}, { ...BASE_ENV, LOVABLE_RECALL_MODE: "answer", LOVABLE_RECALL_ALLOW_ANSWER_MODE: "false" }), /conflicts/);
});

test("redacted config never contains the secrets", () => {
  const config = resolveConfig({}, BASE_ENV);
  const printed = JSON.stringify(redactConfig(config));
  assert.ok(!printed.includes(BASE_ENV.COGNEE_API_KEY));
  assert.ok(!printed.includes(BASE_ENV.LOVABLE_MCP_TOKEN));
});
