/**
 * The manifest's configSchema has `additionalProperties: false`, so OpenClaw's
 * config validation rejects any key it does not list — and doctor then
 * quarantines the WHOLE plugin config, disabling the plugin (#438). So every
 * setting `resolveConfig` reads must be declared there, and nothing declared
 * there may be dead.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { resolveConfig } from "../../src/config";

const root = join(__dirname, "..", "..");
const manifest = JSON.parse(readFileSync(join(root, "openclaw.plugin.json"), "utf-8"));
const schema = manifest.configSchema;
const declared = new Set<string>(Object.keys(schema.properties));
const read = new Set<string>(
  [...readFileSync(join(root, "src", "config.ts"), "utf-8").matchAll(/\braw\.([A-Za-z_]\w*)/g)].map((m) => m[1]),
);

describe("configSchema matches what config.ts reads", () => {
  it("rejects unknown keys, which is why the two must agree", () => {
    expect(schema.additionalProperties).toBe(false);
  });

  it("declares every setting config.ts reads", () => {
    expect([...read].filter((k) => !declared.has(k)).sort()).toEqual([]);
  });

  it("declares nothing config.ts ignores", () => {
    expect([...declared].filter((k) => !read.has(k)).sort()).toEqual([]);
  });

  it("types the settings that were missing (#438) and chunkSize (#428)", () => {
    expect(schema.properties.perAgentMemory.type).toBe("boolean");
    expect(schema.properties.noiseTriggers).toMatchObject({ type: "array", items: { type: "string" } });
    expect(schema.properties.noisePatterns).toMatchObject({ type: "array", items: { type: "string" } });
    expect(schema.properties.chunkSize).toMatchObject({ type: "integer", minimum: 1 });
  });
});

describe("chunkSize (#428)", () => {
  it("is off by default, so the server's own chunk size applies", () => {
    expect(resolveConfig({}).chunkSize).toBe(0);
  });

  it("keeps a positive integer", () => {
    expect(resolveConfig({ chunkSize: 1200 }).chunkSize).toBe(1200);
  });

  it.each([0, -5, 1.5, "1200", null])("treats %p as unset", (value) => {
    expect(resolveConfig({ chunkSize: value } as never).chunkSize).toBe(0);
  });
});
