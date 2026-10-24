/**
 * Event handlers. Events are at-least-once and may arrive out of order, so a
 * handler never trusts the payload: it fetches the authoritative source
 * through the host, checks the company/project allowlist, serializes a
 * bounded record and lets the ledger's unique key collapse duplicates.
 */
import type { PluginContext, PluginEvent } from "@paperclipai/plugin-sdk";
import { resolveConfig } from "./config.js";
import { describeError } from "./errors.js";
import type { Outbox } from "./outbox.js";
import { commentRecord, issueRecord } from "./records.js";
import { ScopePolicy } from "./scope.js";

export interface CaptureDeps {
  ctx: PluginContext;
  outbox: Outbox;
  /** Send right away after enqueueing (the event invocation is the authorized moment). */
  drainNow?: boolean;
}

function payloadString(payload: unknown, ...keys: string[]): string | undefined {
  if (!payload || typeof payload !== "object") return undefined;
  for (const key of keys) {
    const path = key.split(".");
    let cursor: unknown = payload;
    for (const part of path) {
      if (!cursor || typeof cursor !== "object") {
        cursor = undefined;
        break;
      }
      cursor = (cursor as Record<string, unknown>)[part];
    }
    if (typeof cursor === "string" && cursor) return cursor;
  }
  return undefined;
}

export type CaptureOutcome = { captured: boolean; reason: string; rowId?: string; created?: boolean };

export async function handleIssueUpdated(deps: CaptureDeps, event: PluginEvent): Promise<CaptureOutcome> {
  const { ctx, outbox } = deps;
  const issueId = event.entityType === "issue" ? event.entityId : undefined;
  const resolvedIssueId = issueId ?? payloadString(event.payload, "issueId", "issue.id", "id") ?? event.entityId;
  if (!resolvedIssueId) return { captured: false, reason: "no issue id on the event" };
  let config;
  try {
    config = resolveConfig(await ctx.config.get(event.companyId));
  } catch (error) {
    ctx.logger.warn("capture skipped: configuration invalid", { companyId: event.companyId, error: describeError(error) });
    return { captured: false, reason: "configuration invalid" };
  }
  const scope = new ScopePolicy(config);
  if (!scope.isCompanyEnabled(event.companyId)) return { captured: false, reason: "company not enabled" };
  if (config.captureMode !== "completed_issues" && config.captureMode !== "both") return { captured: false, reason: "issue capture disabled" };
  const issue = await ctx.issues.get(resolvedIssueId, event.companyId);
  if (!issue) return { captured: false, reason: "issue not found in this company" };
  if (issue.status !== "done") return { captured: false, reason: `issue status is ${issue.status}` };
  if (!scope.isProjectEnabled(issue.companyId, issue.projectId)) return { captured: false, reason: "project not enabled" };
  const record = issueRecord(config, issue);
  if (!record) return { captured: false, reason: "issue has no project" };
  const { row, created } = await outbox.enqueue(record, scope.projectDataset(issue.companyId, issue.projectId as string));
  if (created) {
    await ctx.activity.log({ companyId: issue.companyId, message: `Queued completed task ${issue.identifier ?? issue.id} for company memory`, entityType: "issue", entityId: issue.id, metadata: { ledgerRowId: row.id } }).catch(() => undefined);
    if (deps.drainNow !== false) await outbox.drainCompany(issue.companyId).catch((error) => ctx.logger.warn("immediate drain failed; the scheduled job will retry", { error: describeError(error) }));
  }
  return { captured: true, reason: created ? "queued" : "already recorded", rowId: row.id, created };
}

export async function handleCommentCreated(deps: CaptureDeps, event: PluginEvent): Promise<CaptureOutcome> {
  const { ctx, outbox } = deps;
  const commentId = (event.entityType === "issue_comment" || event.entityType === "comment" ? event.entityId : undefined) ?? payloadString(event.payload, "commentId", "comment.id", "id") ?? event.entityId;
  const issueId = payloadString(event.payload, "issueId", "comment.issueId", "issue.id");
  if (!commentId) return { captured: false, reason: "no comment id on the event" };
  if (!issueId) return { captured: false, reason: "no issue id on the event payload" };
  let config;
  try {
    config = resolveConfig(await ctx.config.get(event.companyId));
  } catch (error) {
    ctx.logger.warn("capture skipped: configuration invalid", { companyId: event.companyId, error: describeError(error) });
    return { captured: false, reason: "configuration invalid" };
  }
  const scope = new ScopePolicy(config);
  if (!scope.isCompanyEnabled(event.companyId)) return { captured: false, reason: "company not enabled" };
  if (config.captureMode !== "marked_comments" && config.captureMode !== "both") return { captured: false, reason: "comment capture disabled" };
  const issue = await ctx.issues.get(issueId, event.companyId);
  if (!issue) return { captured: false, reason: "issue not found in this company" };
  if (!scope.isProjectEnabled(issue.companyId, issue.projectId)) return { captured: false, reason: "project not enabled" };
  const comments = await ctx.issues.listComments(issue.id, event.companyId);
  const comment = comments.find((entry) => entry.id === commentId);
  if (!comment) return { captured: false, reason: "comment not found on the issue" };
  if (comment.deletedAt) return { captured: false, reason: "comment deleted" };
  const record = commentRecord(config, issue, comment);
  if (!record) return { captured: false, reason: "comment does not start with the decision marker" };
  const { row, created } = await outbox.enqueue(record, scope.projectDataset(issue.companyId, issue.projectId as string));
  if (created) {
    await ctx.activity.log({ companyId: issue.companyId, message: `Queued marked decision from task ${issue.identifier ?? issue.id} for company memory`, entityType: "issue", entityId: issue.id, metadata: { ledgerRowId: row.id, commentId: comment.id } }).catch(() => undefined);
    if (deps.drainNow !== false) await outbox.drainCompany(issue.companyId).catch((error) => ctx.logger.warn("immediate drain failed; the scheduled job will retry", { error: describeError(error) }));
  }
  return { captured: true, reason: created ? "queued" : "already recorded", rowId: row.id, created };
}

/** Operator action: queue every completed issue of one enabled project (backfill or re-sync). */
export async function syncProject(deps: CaptureDeps, companyId: string, projectId: string): Promise<{ queued: number; skipped: number; alreadyRecorded: number }> {
  const { ctx, outbox } = deps;
  const config = resolveConfig(await ctx.config.get(companyId));
  const scope = new ScopePolicy(config);
  if (!scope.isProjectEnabled(companyId, projectId)) throw new Error("project is not enabled for company memory");
  const result = { queued: 0, skipped: 0, alreadyRecorded: 0 };
  const dataset = scope.projectDataset(companyId, projectId);
  for (let offset = 0; ; offset += 100) {
    const issues = await ctx.issues.list({ companyId, projectId, status: "done", limit: 100, offset });
    for (const issue of issues) {
      const record = issueRecord(config, issue);
      if (!record) {
        result.skipped += 1;
        continue;
      }
      const { created } = await outbox.enqueue(record, dataset);
      if (created) result.queued += 1;
      else result.alreadyRecorded += 1;
    }
    if (issues.length < 100) break;
  }
  if (result.queued && deps.drainNow !== false) await outbox.drainCompany(companyId);
  return result;
}
