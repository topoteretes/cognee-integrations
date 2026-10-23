import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { PACKAGE_VERSION, PACKAGE_NAME } from "../dist/index.js";

test("package.json, CHANGELOG and src/version.ts agree on the version", () => {
  const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(pkg.version, PACKAGE_VERSION);
  assert.equal(pkg.name, PACKAGE_NAME);
  const changelog = readFileSync(new URL("../CHANGELOG.md", import.meta.url), "utf8");
  assert.ok(changelog.includes(`## [${PACKAGE_VERSION}]`), `CHANGELOG.md lacks a ## [${PACKAGE_VERSION}] section`);
});
