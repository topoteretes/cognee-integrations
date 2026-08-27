// ---------------------------------------------------------------------------
// Recall session layers + improve status rendering
//
// Pure formatting helpers shared by the prompt-time recall hook and the CLI:
//   * renderSessionLayerSections — turn the session/trace/session_context
//     entries of one recall call into labelled prompt sections, so cached
//     Q&A turns, tool-call lessons and distilled agent guidance reach the model
//     as distinct blocks (the claude-code/codex integrations do the same).
//   * describeImprove — one-line status for an /improve response, whatever
//     shape the server used.
// ---------------------------------------------------------------------------

import type { CogneeImproveResult, CogneeSearchResult } from "./types.js";

/** Section tag per session layer, in the order they are injected. */
export const SESSION_LAYER_SECTIONS: ReadonlyArray<{ source: CogneeSearchResult["source"]; tag: string; heading: string }> = [
  { source: "session_context", tag: "agent_guidance", heading: "Standing guidance distilled from this agent's past sessions" },
  { source: "trace", tag: "trace_lessons", heading: "Lessons from earlier tool calls in this conversation" },
  { source: "session", tag: "session_memory", heading: "Earlier turns of this conversation" },
];

/** Cap per layer so a chatty session can't crowd out the graph results. */
export const MAX_ENTRIES_PER_LAYER = 6;
export const MAX_CHARS_PER_ENTRY = 1_200;

function clip(text: string): string {
  return text.length > MAX_CHARS_PER_ENTRY ? `${text.slice(0, MAX_CHARS_PER_ENTRY)}…` : text;
}

/**
 * Group one recall call's results by session layer and render each non-empty
 * layer as `<tag>` … `</tag>`. Graph/code/tools entries are ignored here —
 * the graph lanes render those. Untagged entries (legacy servers) are treated
 * as session Q&A since only a session-scoped call reaches this function.
 */
export function renderSessionLayerSections(results: readonly CogneeSearchResult[]): string[] {
  const buckets = new Map<string, string[]>();
  for (const r of results) {
    const source = r.source ?? "session";
    if (!SESSION_LAYER_SECTIONS.some((s) => s.source === source)) continue;
    const text = (r.text ?? "").trim();
    if (!text) continue;
    const list = buckets.get(source) ?? [];
    if (list.length >= MAX_ENTRIES_PER_LAYER) continue;
    list.push(clip(text));
    buckets.set(source, list);
  }

  const sections: string[] = [];
  for (const { source, tag, heading } of SESSION_LAYER_SECTIONS) {
    const entries = buckets.get(source as string);
    if (!entries || entries.length === 0) continue;
    sections.push(`<${tag}>\n[${heading}]\n${entries.map((e) => `- ${e.replace(/\n/g, "\n  ")}`).join("\n")}\n</${tag}>`);
  }
  return sections;
}

/** `status=PipelineRunCompleted run=abc… datasets=2` — never `status=?` for a map response. */
export function describeImprove(result: CogneeImproveResult | undefined | null): string {
  if (!result) return "status=?";
  const parts: string[] = [`status=${result.status ?? "?"}`];
  if (result.pipelineRunId) parts.push(`run=${result.pipelineRunId.slice(0, 8)}`);
  const n = result.datasets ? Object.keys(result.datasets).length : 0;
  if (n > 1) parts.push(`datasets=${n}`);
  return parts.join(" ");
}
