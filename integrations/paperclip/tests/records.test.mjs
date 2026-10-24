import test from "node:test";
import assert from "node:assert/strict";
import { resolveConfig, issueRecord, commentRecord, decisionRecord, isMarkedComment, stripMarker, issueLink } from "../dist/lib/index.js";
import { BASE_CONFIG, issue, comment } from "./helpers.mjs";

const config = resolveConfig(BASE_CONFIG);

test("a completed issue becomes a bounded record with provenance and a link; identical content shares a revision", () => {
  const record = issueRecord(config, issue());
  assert.equal(record.sourceType, "issue");
  assert.equal(record.sourceId, issue().id);
  assert.match(record.uploadName, /^issue-[0-9a-f]{32}$/);
  assert.match(record.text, /Task: RES-1 — Interview 12/);
  assert.match(record.text, /Link: https:\/\/paperclip\.example\.com\/issues\/11111111-aaaa-4aaa-8aaa-111111111111/);
  assert.match(record.text, /Source: paperclip:issue:/);
  assert.match(record.text, /Description:\nFindings:/);
  assert.equal(issueRecord(config, issue()).revision, record.revision);
  assert.notEqual(issueRecord(config, issue({ description: "changed" })).revision, record.revision);
  assert.equal(issueRecord(config, issue({ projectId: null })), null);
});

test("records are truncated to the configured budget and links honour the template", () => {
  const small = resolveConfig({ ...BASE_CONFIG, maxRecordChars: 800, issueLinkTemplate: "{publicUrl}/c/{companyId}/p/{projectId}/i/{issueId}" });
  const record = issueRecord(small, issue({ description: "x".repeat(5000) }));
  assert.ok(record.text.length < 1200, `record too long: ${record.text.length}`);
  assert.match(record.text, /\[truncated\]/);
  assert.equal(issueLink(small, { companyId: "c", projectId: "p", issueId: "i" }), "https://paperclip.example.com/c/c/p/p/i/i");
  assert.equal(issueLink(resolveConfig({ ...BASE_CONFIG, paperclipPublicUrl: undefined }), { companyId: "c", projectId: "p", issueId: "i" }), null);
});

test("the decision marker is matched deterministically", () => {
  assert.equal(isMarkedComment("#remember we chose X", "#remember"), true);
  assert.equal(isMarkedComment("  #remember\nwe chose X", "#remember"), true);
  assert.equal(isMarkedComment("#remember", "#remember"), true);
  assert.equal(isMarkedComment("#remembered this", "#remember"), false);
  assert.equal(isMarkedComment("please #remember this", "#remember"), false);
  assert.equal(isMarkedComment("", "#remember"), false);
  assert.equal(stripMarker("#remember  we chose X ", "#remember"), "we chose X");
  const record = commentRecord(config, issue(), comment());
  assert.equal(record.sourceType, "comment");
  assert.match(record.text, /Decision:\nPassword friction/);
  assert.equal(commentRecord(config, issue(), comment({ body: "no marker here" })), null);
});

test("an agent decision's identity is its content, not its run", () => {
  const a = decisionRecord(config, issue(), { content: "Ship magic links.", rationale: "RES-1", agentId: "agent-1", runId: "run-1", recordedAt: new Date("2026-10-23T00:00:00Z") });
  const b = decisionRecord(config, issue(), { content: "Ship magic links.", rationale: "RES-1", agentId: "agent-2", runId: "run-2", recordedAt: new Date("2026-10-24T00:00:00Z") });
  assert.equal(a.sourceId, b.sourceId);
  assert.equal(a.revision, b.revision, "the run stamp is attribution, not identity: a replay is the same revision");
  assert.equal(a.uploadName, b.uploadName);
  assert.notEqual(decisionRecord(config, issue(), { content: "Ship passwords.", agentId: "a", runId: "r", recordedAt: new Date() }).revision, a.revision);
  assert.match(a.text, /Rationale:\nRES-1/);
  assert.equal(decisionRecord(config, issue(), { content: "   ", agentId: "a", runId: "r", recordedAt: new Date() }), null);
});
