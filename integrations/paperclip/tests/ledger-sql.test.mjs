import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { PluginDbLedger, LEDGER_TABLE, LIVE_STATUSES } from "../dist/lib/index.js";

/** A scripted ctx.db: records SQL, returns what the test queues. */
function fakeDb() {
  const queries = [];
  const executes = [];
  const responses = [];
  return {
    queries,
    executes,
    respond: (rows) => responses.push(rows),
    db: {
      async query(sql, params) {
        queries.push({ sql, params });
        return responses.shift() ?? [];
      },
      async execute(sql, params) {
        executes.push({ sql, params });
        return { rowCount: 1 };
      },
    },
  };
}

const row = {
  id: "r1", company_id: "c", project_id: "p", source_type: "issue", source_id: "i", revision: "rev", dataset: "pc_x", upload_name: "issue-rev", content: "text", source_ref: "paperclip:issue:i", link: null,
  status: "pending", dataset_id: null, data_id: null, error: null, attempts: 0, created_at: new Date("2026-10-23T00:00:00Z"), updated_at: new Date("2026-10-23T00:00:00Z"), last_checked_at: null,
};

test("upsert is INSERT ... ON CONFLICT DO NOTHING on the unique source/revision key, followed by a SELECT", async () => {
  const fake = fakeDb();
  fake.respond([row]);
  const ledger = new PluginDbLedger(fake.db, () => new Date("2026-10-23T00:00:00Z"));
  const { row: got, created } = await ledger.upsert({ companyId: "c", projectId: "p", sourceType: "issue", sourceId: "i", revision: "rev", dataset: "pc_x", uploadName: "issue-rev", content: "text", sourceRef: "paperclip:issue:i", link: null });
  assert.match(fake.executes[0].sql, new RegExp(`INSERT INTO ${LEDGER_TABLE} .*ON CONFLICT \\(company_id, source_type, source_id, revision\\) DO NOTHING`, "s"));
  assert.ok(fake.executes[0].sql.includes("$12, $12"), "created_at and updated_at share one timestamp parameter");
  assert.equal(created, true);
  assert.equal(got.id, "r1");
  assert.equal(got.createdAt, "2026-10-23T00:00:00.000Z");
});

test("update builds a positional SET list and bumps attempts; list filters are parameterised", async () => {
  const fake = fakeDb();
  const ledger = new PluginDbLedger(fake.db);
  fake.respond([{ ...row, status: "accepted" }]);
  await ledger.update("r1", { status: "accepted", datasetId: "d", bumpAttempts: true });
  assert.match(fake.executes[0].sql, /UPDATE memory_ledger SET status = \$1, dataset_id = \$2, attempts = attempts \+ 1, updated_at = \$3 WHERE id = \$4/);
  assert.deepEqual(fake.executes[0].params.slice(0, 2), ["accepted", "d"]);
  await ledger.list({ companyId: "c", status: ["pending", "superseded"], limit: 10 });
  assert.match(fake.queries.at(-1).sql, /WHERE company_id = \$1 AND status IN \(\$2, \$3\) ORDER BY created_at ASC LIMIT \$4/);
  assert.deepEqual(fake.queries.at(-1).params, ["c", "pending", "superseded", 10]);
  await ledger.otherRevisions("c", "issue", "i", "rev");
  assert.ok(fake.queries.at(-1).sql.includes("revision <> $4"));
  assert.equal(fake.queries.at(-1).params.length, 4 + LIVE_STATUSES.length);
});

test("the migration creates the table with the unique constraint the ledger relies on", () => {
  const sql = readFileSync(new URL("../migrations/001_memory_ledger.sql", import.meta.url), "utf8");
  assert.match(sql, /CREATE TABLE IF NOT EXISTS memory_ledger/);
  assert.match(sql, /UNIQUE \(company_id, source_type, source_id, revision\)/);
  for (const column of ["upload_name", "dataset_id", "data_id", "status", "attempts", "last_checked_at"]) assert.ok(sql.includes(column), column);
});
