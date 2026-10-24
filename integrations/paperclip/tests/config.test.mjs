import test from "node:test";
import assert from "node:assert/strict";
import { resolveConfig, validateConfig, redactConfig, ConfigError, ScopePolicy, datasetFor, manifest, PACKAGE_VERSION } from "../dist/lib/index.js";
import { BASE_CONFIG, COMPANY, OTHER_COMPANY, PROJECT, PROJECT_OFF } from "./helpers.mjs";

test("required settings are reported together; warnings do not block", () => {
  assert.throws(() => resolveConfig({}), (error) => {
    assert.ok(error instanceof ConfigError);
    assert.match(error.message, /cogneeBaseUrl/);
    assert.match(error.message, /cogneeApiKeyRef/);
    return true;
  });
  const result = validateConfig({ ...BASE_CONFIG, enabledProjectIds: [] });
  assert.equal(result.ok, true);
  assert.ok(result.warnings.some((w) => /enabledProjectIds is empty/.test(w)));
});

test("defaults, trimming and bounds", () => {
  const config = resolveConfig({ ...BASE_CONFIG, cogneeBaseUrl: "http://cognee.test:8000/", enabledProjectIds: ` ${PROJECT} , ${PROJECT}` });
  assert.equal(config.cogneeBaseUrl, "http://cognee.test:8000");
  assert.deepEqual(config.enabledProjectIds, [PROJECT]);
  assert.equal(config.captureMode, "both");
  assert.equal(config.decisionMarker, "#remember");
  assert.equal(config.recallTopK, 5);
  assert.throws(() => resolveConfig({ ...BASE_CONFIG, captureMode: "everything" }), /captureMode/);
  assert.throws(() => resolveConfig({ ...BASE_CONFIG, decisionMarker: "re member" }), /whitespace/);
  assert.throws(() => resolveConfig({ ...BASE_CONFIG, recallTopK: 99 }), /recallTopK/);
  assert.throws(() => resolveConfig({ ...BASE_CONFIG, companySharedDataset: "bad name" }), /dataset name/);
});

test("the secret reference never appears in the redacted config", () => {
  const printed = JSON.stringify(redactConfig(resolveConfig(BASE_CONFIG)));
  assert.ok(!printed.includes("secret-1"));
});

test("scope: allowlists decide; datasets are opaque and stable per instance+company+project", () => {
  const scope = new ScopePolicy(resolveConfig({ ...BASE_CONFIG, companySharedDataset: "northwind_shared" }));
  assert.equal(scope.isCompanyEnabled(COMPANY), true);
  assert.equal(scope.isCompanyEnabled(OTHER_COMPANY), false);
  assert.equal(scope.isProjectEnabled(COMPANY, PROJECT), true);
  assert.equal(scope.isProjectEnabled(COMPANY, PROJECT_OFF), false);
  assert.equal(scope.isProjectEnabled(OTHER_COMPANY, PROJECT), false);
  const ds = scope.projectDataset(COMPANY, PROJECT);
  assert.match(ds, /^pc_[0-9a-f]{16}$/);
  assert.equal(ds, datasetFor("paperclip", COMPANY, PROJECT));
  assert.notEqual(ds, datasetFor("other-instance", COMPANY, PROJECT));
  assert.deepEqual(scope.readDatasetsFor(COMPANY, PROJECT), [ds, "northwind_shared"]);
  assert.deepEqual(scope.readDatasetsFor(COMPANY, PROJECT_OFF), ["northwind_shared"]);
  assert.deepEqual(scope.readDatasetsFor(OTHER_COMPANY, PROJECT), []);
  const { kept, dropped } = scope.filterReadable([{ dataset_name: ds }, { dataset_name: "pc_someoneelse" }, {}], [ds]);
  assert.equal(kept.length, 1);
  assert.equal(dropped, 2);
});

test("manifest declares only read/outbound/tool/job/db capabilities and matches the package version", () => {
  assert.equal(manifest.version, PACKAGE_VERSION);
  assert.equal(manifest.id, "cognee.company-memory");
  for (const forbidden of ["issues.update", "issues.create", "agents.invoke", "issue.comments.create", "approvals.respond"]) assert.ok(!manifest.capabilities.includes(forbidden), forbidden);
  assert.deepEqual(manifest.tools.map((t) => t.name).sort(), ["cognee_recall", "cognee_remember_decision"]);
  assert.deepEqual(manifest.jobs.map((j) => j.jobKey).sort(), ["drain-outbox", "reconcile-sources"]);
  assert.equal(manifest.database.migrationsDir, "migrations");
  assert.ok(manifest.instanceConfigSchema.required.includes("cogneeApiKeyRef"));
});
