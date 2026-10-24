import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { PACKAGE_VERSION, PACKAGE_NAME, PINNED_SDK_VERSION } from "../dist/lib/index.js";

test("package.json, CHANGELOG, src/version.ts and the built manifest agree", async () => {
  const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(pkg.version, PACKAGE_VERSION);
  assert.equal(pkg.name, PACKAGE_NAME);
  assert.equal(pkg.dependencies["@paperclipai/plugin-sdk"], PINNED_SDK_VERSION);
  assert.deepEqual(pkg.paperclipPlugin, { manifest: "dist/manifest.js", worker: "dist/worker.js" });
  const changelog = readFileSync(new URL("../CHANGELOG.md", import.meta.url), "utf8");
  assert.ok(changelog.includes(`## [${PACKAGE_VERSION}]`));
  const manifest = (await import("../dist/manifest.js")).default;
  assert.equal(manifest.version, PACKAGE_VERSION);
  assert.ok(existsSync(new URL("../dist/worker.js", import.meta.url)));
  assert.ok(existsSync(new URL("../migrations/001_memory_ledger.sql", import.meta.url)));
  assert.ok(existsSync(new URL("../skills/company-memory/SKILL.md", import.meta.url)));
});
