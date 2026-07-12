/**
 * Build a memory anchor before context-window compaction.
 *
 * Ported from pre-compact.py (renamed to post-compact). Pulls a compact
 * summary from three session-cache layers — recent QAs, per-step trace
 * feedback, and the graph-context snapshot — and emits a markdown block
 * the compactor preserves.
 *
 * Best-effort: never throws from hooks.
 */

import {
  loadConfig,
  hookLog,
  getSessionKey,
  resolveSessionId,
  sanitizeSessionKey,
  resolveHttpEndpoint,
} from "./plugin-common.ts";
import {
  recall,
  breakerOpen,
  UNREACHABLE,
  type RecallResult,
} from "./cognee-client.ts";

// ─── Constants ────────────────────────────────────────────────────────────────

const MIN_WORD_LEN = 3;
const SESSION_TOP_K = 5;
const TRACE_TOP_K = 8;
const GRAPH_TOP_K = 3;

// ─── Types ────────────────────────────────────────────────────────────────────

type RecallEntry = Record<string, unknown>;

// ─── Helpers ──────────────────────────────────────────────────────────────────

function extractQueryWords(entries: RecallEntry[], maxWords = 20): string {
  const words: string[] = [];
  const recent = entries.slice(-3);
  for (const entry of recent) {
    const blob = ["question", "answer", "origin_function", "session_feedback"]
      .map((f) => String(entry[f] ?? ""))
      .join(" ");
    const matches = blob.toLowerCase().match(/\b\w+\b/g) ?? [];
    for (const w of matches) {
      if (w.length >= MIN_WORD_LEN) {
        words.push(w);
        if (words.length >= maxWords) return words.join(" ");
      }
    }
  }
  return words.join(" ");
}

function normalizeResults(raw: RecallResult): RecallEntry[] {
  if (raw === UNREACHABLE) return [];
  if (Array.isArray(raw)) {
    return raw.filter((r): r is RecallEntry => typeof r === "object" && r !== null);
  }
  return [];
}

// ─── Section formatters ───────────────────────────────────────────────────────

function formatSessionSection(entries: RecallEntry[]): string {
  const lines = ["### Session Memory (recent turns)"];
  for (const entry of entries) {
    const q = String(entry.question ?? "").trim();
    const a = String(entry.answer ?? "").trim();
    if (!q && !a) continue;
    const source = q || a;
    let short = source.slice(0, 300);
    if (source.length > 300) short += "...";
    const prefix = q ? "Q: " : "A: ";
    lines.push(`- ${prefix}${short}`);
  }
  return lines.length > 1 ? lines.join("\n") : "";
}

function formatTraceSection(entries: RecallEntry[]): string {
  const lines = ["### Agent Trace (tool calls & feedback)"];
  for (const entry of entries) {
    const origin = String(entry.origin_function ?? "?");
    const status = String(entry.status ?? "");
    const feedback = String(entry.session_feedback ?? "").trim();
    if (feedback) {
      lines.push(`- ${origin} [${status}]: ${feedback.slice(0, 200)}`);
    } else {
      lines.push(`- ${origin} [${status}]`);
    }
  }
  return lines.length > 1 ? lines.join("\n") : "";
}

function formatGraphContextSection(entries: RecallEntry[]): string {
  const lines = ["### Knowledge Graph Snapshot"];
  for (const entry of entries) {
    const content = String(entry.content ?? entry.answer ?? entry.text ?? "");
    let short = content.slice(0, 400);
    if (content.length > 400) short += "...";
    if (short.trim()) lines.push(short);
  }
  return lines.length > 1 ? lines.join("\n") : "";
}

function formatGraphSection(entries: RecallEntry[]): string {
  const lines = ["### Knowledge Graph (search hits)"];
  for (const entry of entries) {
    if (typeof entry !== "object") {
      lines.push(`- ${String(entry).slice(0, 300)}`);
      continue;
    }
    const text = String(entry.answer ?? entry.text ?? entry.content ?? JSON.stringify(entry));
    const short = text.length > 300 ? text.slice(0, 300) + "..." : text;
    lines.push(`- ${short}`);
  }
  return lines.length > 1 ? lines.join("\n") : "";
}

// ─── Recall wrapper ───────────────────────────────────────────────────────────

async function recallScope(
  baseUrl: string,
  apiKey: string,
  sessionId: string,
  dataset: string,
  query: string,
  scope: string[],
  topK: number,
): Promise<RecallEntry[]> {
  try {
    const result = await recall(baseUrl, apiKey, query, sessionId, scope, topK, dataset);
    return normalizeResults(result);
  } catch (err) {
    hookLog("precompact_recall_error", { scope, error: String(err).slice(0, 200) });
    return [];
  }
}

// ─── Main entry point ─────────────────────────────────────────────────────────

/**
 * Build a memory anchor for injection after context-window compaction.
 *
 * @param conversationId The Vellum conversation ID (host session key).
 * @returns A markdown anchor block, or null if no memory to anchor.
 */
export async function postCompact(conversationId: string): Promise<string | null> {
  const cfg = loadConfig();
  const sessionKey = sanitizeSessionKey(conversationId);
  if (sessionKey) {
    process.env.COGNEE_SESSION_KEY = sessionKey;
  }

  const sessionId = resolveSessionId(sessionKey, cfg.agentName);
  const dataset = cfg.dataset;
  if (!sessionId) {
    hookLog("no_session_id", { event: "precompact" });
    return null;
  }

  const { baseUrl, apiKey } = resolveHttpEndpoint();
  if (!apiKey) {
    hookLog("precompact_no_api_key");
    return null;
  }

  // Respect the circuit breaker.
  const [breakerOpen_, _retryIn] = breakerOpen();
  if (breakerOpen_) {
    hookLog("precompact_breaker_open");
    return null;
  }

  // First pull session + trace so we can derive a query from them.
  const seedResults = await recallScope(
    baseUrl,
    apiKey,
    sessionId,
    dataset,
    "",
    ["session", "trace"],
    TRACE_TOP_K,
  );

  let sessionEntries = seedResults.filter(
    (r) => String(r.source ?? r._source ?? "") === "session",
  );
  let traceEntries = seedResults.filter(
    (r) => String(r.source ?? r._source ?? "") === "trace",
  );

  // Truncate to top-K most recent.
  sessionEntries = sessionEntries.slice(-SESSION_TOP_K);
  traceEntries = traceEntries.slice(-TRACE_TOP_K);

  // Derive a query from the recent activity for graph-context search.
  const query = extractQueryWords([...sessionEntries, ...traceEntries]);

  let graphContextEntries: RecallEntry[] = [];
  let graphEntries: RecallEntry[] = [];

  if (query) {
    const ctx = await recallScope(
      baseUrl,
      apiKey,
      sessionId,
      dataset,
      query,
      ["graph_context"],
      1,
    );
    graphContextEntries = ctx;

    const g = await recallScope(
      baseUrl,
      apiKey,
      sessionId,
      dataset,
      query,
      ["graph"],
      GRAPH_TOP_K,
    );
    // Fold graph results into graph_context for display.
    for (const entry of g) {
      entry.source = "graph_context";
    }
    graphEntries = g;
  }

  // Build sections in display order.
  const sections: string[] = [];
  if (sessionEntries.length) {
    const s = formatSessionSection(sessionEntries);
    if (s) sections.push(s);
  }
  if (traceEntries.length) {
    const s = formatTraceSection(traceEntries);
    if (s) sections.push(s);
  }
  if (graphContextEntries.length) {
    const s = formatGraphContextSection(graphContextEntries);
    if (s) sections.push(s);
  }
  if (graphEntries.length) {
    const s = formatGraphSection(graphEntries);
    if (s) sections.push(s);
  }

  if (sections.length === 0) {
    hookLog("precompact_empty");
    return null;
  }

  const header =
    "## Cognee Memory Anchor\n" +
    "Preserved context from session, agent trace, and knowledge graph:\n";
  const anchor = header + sections.join("\n\n");

  hookLog("precompact_anchor", {
    session_entries: sessionEntries.length,
    trace_entries: traceEntries.length,
    graph_context: graphContextEntries.length,
    graph: graphEntries.length,
  });

  return anchor;
}

export default postCompact;
