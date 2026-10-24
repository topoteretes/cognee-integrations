/**
 * Source records: the bounded text that is stored in cognee for one completed
 * issue, one marked comment, or one explicit agent decision. Provenance lives
 * in the text (ids, author, timestamp, link) so it survives any backend
 * version's metadata handling; the upload name is derived from the content
 * hash so identical content is a no-op and a reused name never conflicts.
 */
import { createHash } from "node:crypto";
import type { Issue, IssueComment } from "@paperclipai/plugin-sdk";
import type { PluginConfig } from "./config.js";

export type SourceType = "issue" | "comment" | "decision";

export interface SourceRecord {
  companyId: string;
  projectId: string;
  sourceType: SourceType;
  sourceId: string;
  /** Content hash: changes whenever the serialized record changes. */
  revision: string;
  uploadName: string;
  text: string;
  sourceRef: string;
  link: string | null;
}

export function hashText(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

export function normalizeText(value: string | null | undefined, maxChars: number): string {
  const text = (value ?? "").replace(/\r\n?/g, "\n").trim();
  return text.length > maxChars ? `${text.slice(0, maxChars)}\n[truncated]` : text;
}

export function issueLink(config: PluginConfig, ids: { companyId: string; projectId: string | null; issueId: string }): string | null {
  if (!config.paperclipPublicUrl) return null;
  return config.issueLinkTemplate
    .replace("{publicUrl}", config.paperclipPublicUrl)
    .replace("{companyId}", encodeURIComponent(ids.companyId))
    .replace("{projectId}", encodeURIComponent(ids.projectId ?? ""))
    .replace("{issueId}", encodeURIComponent(ids.issueId));
}

function stamp(value: unknown): string {
  if (value instanceof Date) return value.toISOString();
  if (typeof value === "string" && value) return value;
  return "unknown";
}

function finish(partial: Omit<SourceRecord, "revision" | "uploadName">): SourceRecord {
  const revision = hashText(partial.text);
  return { ...partial, revision, uploadName: `${partial.sourceType}-${revision.slice(0, 32)}` };
}

/** A completed issue: title, description, who, when, link. */
export function issueRecord(config: PluginConfig, issue: Issue): SourceRecord | null {
  if (!issue.projectId) return null;
  const budget = Math.max(200, config.maxRecordChars - 600);
  const link = issueLink(config, { companyId: issue.companyId, projectId: issue.projectId, issueId: issue.id });
  const label = issue.identifier ? `${issue.identifier} — ${issue.title}` : issue.title;
  const lines = [
    `Paperclip completed task (company ${issue.companyId}, project ${issue.projectId})`,
    `Task: ${label}`,
    `Task id: ${issue.id}`,
    `Status: ${issue.status}; last updated ${stamp((issue as { updatedAt?: unknown }).updatedAt)}`,
    `Assignee agent: ${issue.assigneeAgentId ?? "none"}; created by agent ${issue.createdByAgentId ?? "none"} / user ${issue.createdByUserId ?? "none"}`,
    `Link: ${link ?? "(no public URL configured)"}`,
    `Source: paperclip:issue:${issue.id}`,
    "",
    "Description:",
    normalizeText(issue.description, budget) || "(no description)",
  ];
  return finish({
    companyId: issue.companyId,
    projectId: issue.projectId,
    sourceType: "issue",
    sourceId: issue.id,
    text: `${lines.join("\n")}\n`,
    sourceRef: `paperclip:issue:${issue.id}`,
    link,
  });
}

/** Does a comment body carry the explicit memory marker? Deterministic: marker first, then whitespace or end. */
export function isMarkedComment(body: string | null | undefined, marker: string): boolean {
  if (!body) return false;
  const trimmed = body.trimStart();
  if (!trimmed.startsWith(marker)) return false;
  const rest = trimmed.slice(marker.length);
  return rest === "" || /^\s/.test(rest);
}

export function stripMarker(body: string, marker: string): string {
  const trimmed = body.trimStart();
  return trimmed.startsWith(marker) ? trimmed.slice(marker.length).trim() : trimmed.trim();
}

/** A comment that starts with the decision marker, stored with its issue context. */
export function commentRecord(config: PluginConfig, issue: Issue, comment: IssueComment): SourceRecord | null {
  if (!issue.projectId) return null;
  if (!isMarkedComment(comment.body, config.decisionMarker)) return null;
  const budget = Math.max(200, config.maxRecordChars - 600);
  const link = issueLink(config, { companyId: issue.companyId, projectId: issue.projectId, issueId: issue.id });
  const author = comment.authorAgentId ? `agent ${comment.authorAgentId}` : comment.authorUserId ? `user ${comment.authorUserId}` : comment.authorType;
  const lines = [
    `Paperclip recorded decision (company ${issue.companyId}, project ${issue.projectId})`,
    `Task: ${issue.identifier ? `${issue.identifier} — ${issue.title}` : issue.title}`,
    `Task id: ${issue.id}; comment id: ${comment.id}`,
    `Author: ${author}; recorded ${stamp(comment.createdAt)}`,
    `Link: ${link ?? "(no public URL configured)"}`,
    `Source: paperclip:comment:${comment.id}`,
    "",
    "Decision:",
    normalizeText(stripMarker(comment.body, config.decisionMarker), budget) || "(empty)",
  ];
  return finish({
    companyId: issue.companyId,
    projectId: issue.projectId,
    sourceType: "comment",
    sourceId: comment.id,
    text: `${lines.join("\n")}\n`,
    sourceRef: `paperclip:comment:${comment.id}`,
    link,
  });
}

/** An explicit decision an agent saved through the tool, tied to an authorized issue. */
export function decisionRecord(
  config: PluginConfig,
  issue: Issue,
  input: { content: string; rationale?: string; agentId: string; runId: string; recordedAt: Date },
): SourceRecord | null {
  if (!issue.projectId) return null;
  const content = normalizeText(input.content, Math.max(200, config.maxRecordChars - 1200));
  if (!content) return null;
  const rationale = normalizeText(input.rationale, Math.min(3000, Math.max(200, config.maxRecordChars - 1200)));
  const link = issueLink(config, { companyId: issue.companyId, projectId: issue.projectId, issueId: issue.id });
  const lines = [
    `Paperclip agent decision (company ${issue.companyId}, project ${issue.projectId})`,
    `Task: ${issue.identifier ? `${issue.identifier} — ${issue.title}` : issue.title}`,
    `Task id: ${issue.id}`,
    `Recorded by agent ${input.agentId} in run ${input.runId} at ${input.recordedAt.toISOString()}`,
    `Link: ${link ?? "(no public URL configured)"}`,
    `Source: paperclip:issue:${issue.id}:run:${input.runId}`,
    "",
    "Decision:",
    content,
  ];
  if (rationale) lines.push("", "Rationale:", rationale);
  const text = `${lines.join("\n")}\n`;
  // Identity AND revision come from the content, not the run or the timestamp:
  // the same decision saved twice (by any agent, in any run) is one record, so a
  // replay never retires and re-sends an identical document.
  const identity = hashText(`${issue.id}\u0000${content}\u0000${rationale}`);
  return {
    companyId: issue.companyId,
    projectId: issue.projectId,
    sourceType: "decision",
    sourceId: `${issue.id}:${identity.slice(0, 32)}`,
    revision: identity,
    uploadName: `decision-${identity.slice(0, 32)}`,
    text,
    sourceRef: `paperclip:issue:${issue.id}:run:${input.runId}`,
    link,
  };
}
