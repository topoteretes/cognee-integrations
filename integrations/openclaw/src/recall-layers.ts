// ---------------------------------------------------------------------------
// Improve status rendering
//
//   * describeImprove — one-line status for an /improve response, whatever
//     shape the server used.
//
// The session-layer renderer that used to live here is gone: since cognee
// 1.6.0 the prompt-time recall is ONE graph-scope only_context request whose
// `text` already carries the conversation history, the retrieved context and
// the session guidance, injected verbatim by the recall hook in plugin.ts.
// ---------------------------------------------------------------------------

import type { CogneeImproveResult } from "./types.js";

/** `status=PipelineRunCompleted run=abc… datasets=2` — never `status=?` for a map response. */
export function describeImprove(result: CogneeImproveResult | undefined | null): string {
  if (!result) return "status=?";
  const parts: string[] = [`status=${result.status ?? "?"}`];
  if (result.pipelineRunId) parts.push(`run=${result.pipelineRunId.slice(0, 8)}`);
  const n = result.datasets ? Object.keys(result.datasets).length : 0;
  if (n > 1) parts.push(`datasets=${n}`);
  if (result.error) parts.push(`(${result.error})`);
  return parts.join(" ");
}
