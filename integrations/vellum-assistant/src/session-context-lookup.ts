/**
 * Search session + trace + graph-context for context relevant to the user's prompt.
 *
 * Ported from session-context-lookup.py. Runs on the UserPromptSubmit hook.
 * Calls `recall()` with multiple scopes so every layer the session manager
 * holds (QA entries, agent trace steps, distilled graph-knowledge snapshot)
 * flows back into the model's context.
 *
 * Best-effort: never throws from hooks.
 */

import { join } from "node:path";
import { writeFileSync, mkdirSync, readFileSync } from "node:fs";

import {
  loadConfig,
  hookLog,
  getSessionKey,
  resolveSessionId,
  sanitizeSessionKey,
  resolveHttpEndpoint,
  isServerReady,
  markServerReady,
  pluginStateDir,
} from "./plugin-common.ts";
import {
  recall,
  breakerOpen,
  UNREACHABLE,
  type RecallResult,
} from "./cognee-client.ts";

// ─── Constants ────────────────────────────────────────────────────────────────

const TOP_K = 5;
const TRUNCATE_ANSWER = 500;
const TRUNCATE_RETURN = 400;
const TRUNCATE_GRAPH_CTX = 1500;

function floatEnv(name: string, def: number): number {
  const raw = process.env[name];
  if (!raw) return def;
  const n = Number(raw);
  return Number.isFinite(n) ? n : def;
}

// ─── Entry formatting ─────────────────────────────────────────────────────────

type RecallEntry = Record<string, unknown>;

function hasEntryContent(entry: RecallEntry): boolean {
  const source = String(entry.source ?? "");
  if (source === "graph_context" || source === "session_context") {
    return Boolean(String(entry.content ?? entry.text ?? "").trim());
  }
  if (source === "trace") {
    return ["origin_function", "status", "session_feedback", "method_return_value"].some(
      (f) => Boolean(String(entry[f] ?? "").trim()),
    );
  }
  return Boolean(String(entry.question ?? "").trim()) || Boolean(String(entry.answer ?? "").trim());
}

function truncateStr(value: unknown, cap: number): string {
  if (value === null || value === undefined) return "";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  if (text.length <= cap) return text;
  return text.slice(0, cap - 3) + "...";
}

function formatEntry(entry: RecallEntry): string {
  const source = String(entry.source ?? "");

  if (source === "graph_context") {
    const content = truncateStr(entry.content ?? entry.text, TRUNCATE_GRAPH_CTX);
    return `[graph-snapshot]\n${content}`;
  }

  if (source === "session_context") {
    const content = truncateStr(entry.content ?? entry.text, TRUNCATE_GRAPH_CTX);
    return `[agent-guidance]\n${content}`;
  }

  if (source === "trace") {
    const origin = String(entry.origin_function ?? "?");
    const status = String(entry.status ?? "");
    const feedback = String(entry.session_feedback ?? "");
    const mrv = truncateStr(entry.method_return_value, TRUNCATE_RETURN);
    const parts = [`[trace] ${origin} — ${status}`];
    if (feedback) parts.push(`  feedback: ${feedback}`);
    if (mrv) parts.push(`  output: ${mrv}`);
    return parts.join("\n");
  }

  // session (QA) or generic
  const q = String(entry.question ?? "");
  const a = String(entry.answer ?? "");
  const t = String(entry.time ?? "");
  const lines: string[] = [];
  if (q) lines.push(`[${t}] Q: ${q}`);
  if (a) {
    const aShort = a.length > TRUNCATE_ANSWER ? a.slice(0, TRUNCATE_ANSWER) + "..." : a;
    lines.push(`A: ${aShort}`);
  }
  return lines.join("\n");
}

// ─── Result normalization ─────────────────────────────────────────────────────

function normalizeResults(raw: RecallResult): RecallEntry[] {
  if (raw === UNREACHABLE) return [];
  if (Array.isArray(raw)) {
    return raw.filter((r): r is RecallEntry => typeof r === "object" && r !== null);
  }
  // Error envelope — log and return empty.
  if (typeof raw === "object" && raw !== null && "error" in raw) {
    hookLog("recall_error_envelope", { error: String((raw as Record<string, unknown>).error).slice(0, 200) });
    return [];
  }
  return [];
}

// ─── Save counter (last-turn stats for the header) ────────────────────────────

interface SaveCounts {
  prompt: number;
  trace: number;
  answer: number;
}

function readAndResetSaveCounter(): SaveCounts {
  // The plugin-common bumpSaveCounter writes a counter file; we read it for
  // display purposes and reset it. Mirrors `read_and_reset_save_counter`.
  try {
    const path = join(pluginStateDir(), "save_counter.json");
    const data = JSON.parse(readFileSync(path, "utf-8")) as Record<string, unknown>;
    const kinds = (data.kinds ?? {}) as Record<string, number>;
    // Reset the file so next turn starts fresh.
    const reset = { count: 0, last_improve: data.last_improve ?? 0, kinds: { prompt: 0, trace: 0, answer: 0 } };
    writeFileSync(path, JSON.stringify(reset), "utf-8");
    return {
      prompt: kinds.prompt ?? 0,
      trace: kinds.trace ?? 0,
      answer: kinds.answer ?? 0,
    };
  } catch {
    return { prompt: 0, trace: 0, answer: 0 };
  }
}

// ─── Main search function ─────────────────────────────────────────────────────

/**
 * Search Cognee memory for context relevant to the user's prompt.
 *
 * @param prompt      The user's prompt text.
 * @param sessionId   The Cognee session ID.
 * @param dataset     The dataset name.
 * @returns           Formatted markdown context block, or null if no results.
 */
export async function searchContext(
  prompt: string,
  sessionId: string,
  dataset: string,
): Promise<string | null> {
  if (!prompt || prompt.length < 5) return null;
  if (!sessionId) {
    hookLog("no_session_id", { event: "context_lookup" });
    return null;
  }

  const { baseUrl, apiKey } = resolveHttpEndpoint();
  if (!apiKey) {
    hookLog("context_lookup_no_api_key");
    return null;
  }

  // Readiness gate: don't block the prompt on a warming backend.
  if (!isServerReady()) {
    // Do one short health probe.
    const { backendReachable } = await import("./cognee-client.ts");
    if (await backendReachable(baseUrl, floatEnv("COGNEE_READY_PROBE_TIMEOUT", 1.0) * 1000)) {
      markServerReady();
    } else {
      hookLog("recall_skipped_warming", { baseUrl });
      return null;
    }
  }

  // Respect the circuit breaker.
  const [breakerOpen_, retryIn] = breakerOpen();
  if (breakerOpen_) {
    hookLog("recall_breaker_open", { retry_in: retryIn });
    return null;
  }

  // Run scopes independently: a failure in one must not discard hits from others.
  const scopeSpecs: Array<{ scope: string[]; topK: number }> = [
    { scope: ["session"], topK: TOP_K },
    { scope: ["trace"], topK: TOP_K },
    { scope: ["graph_context"], topK: TOP_K },
    { scope: ["graph"], topK: TOP_K },
    { scope: ["session_context"], topK: TOP_K },
  ];

  const recallTimeout = floatEnv("COGNEE_RECALL_TIMEOUT", 2.5) * 1000;
  const budgetMs = floatEnv("COGNEE_RECALL_BUDGET", 4.0) * 1000;
  const budgetDeadline = Date.now() + budgetMs;

  const allResults: RecallEntry[] = [];

  for (const { scope, topK } of scopeSpecs) {
    if (Date.now() >= budgetDeadline) {
      hookLog("recall_budget_exceeded", { collected: allResults.length });
      break;
    }
    try {
      const part = await recall(
        baseUrl,
        apiKey,
        prompt,
        sessionId,
        scope,
        topK,
        dataset,
        recallTimeout,
      );
      const entries = normalizeResults(part);
      allResults.push(...entries);
    } catch (err) {
      hookLog("recall_error", { scope, error: String(err).slice(0, 200) });
    }
  }

  // Bucket results by source for human-readable output.
  const bySource: Record<string, RecallEntry[]> = {
    session: [],
    trace: [],
    graph_context: [],
    session_context: [],
  };

  for (const r of allResults) {
    let src = String(r.source ?? "session");
    // Fold scope=graph results into graph_context bucket.
    if (src === "graph") {
      r.source = "graph_context";
      src = "graph_context";
    }
    if (!hasEntryContent(r)) continue;
    if (!bySource[src]) bySource[src] = [];
    bySource[src].push(r);
  }

  const counts = {
    session: bySource.session.length,
    trace: bySource.trace.length,
    graph_context: bySource.graph_context.length,
    session_context: bySource.session_context.length,
  };
  const total = counts.session + counts.trace + counts.graph_context + counts.session_context;

  const savesLastTurn = readAndResetSaveCounter();

  // Write last-turn counts for status line rendering (best-effort).
  try {
    const statePath = join(pluginStateDir(), "last_recall.json");
    mkdirSync(pluginStateDir(), { recursive: true });
    writeFileSync(
      statePath,
      JSON.stringify({
        session_id: sessionId,
        ts: new Date().toISOString(),
        hits: counts,
        saves_last_turn: savesLastTurn,
      }),
      "utf-8",
    );
  } catch (err) {
    hookLog("last_recall_write_failed", { error: String(err).slice(0, 200) });
  }

  // Build the visibility header.
  const header =
    `Cognee memory: recall ` +
    `${counts.session} session / ${counts.trace} trace / ` +
    `${counts.graph_context} graph / ${counts.session_context} agent; saved last turn ` +
    `${savesLastTurn.prompt} prompt / ${savesLastTurn.trace} trace / ` +
    `${savesLastTurn.answer} answer`;

  // Build section lines in display order.
  const sectionLines: string[] = [];
  if (bySource.session_context.length) {
    sectionLines.push("=== Active agent guidance ===");
    for (const e of bySource.session_context) {
      sectionLines.push(formatEntry(e));
      sectionLines.push("");
    }
  }
  if (bySource.graph_context.length) {
    sectionLines.push("=== Knowledge graph snapshot ===");
    for (const e of bySource.graph_context) {
      sectionLines.push(formatEntry(e));
      sectionLines.push("");
    }
  }
  if (bySource.trace.length) {
    sectionLines.push("=== Prior agent trace ===");
    for (const e of bySource.trace) {
      sectionLines.push(formatEntry(e));
      sectionLines.push("");
    }
  }
  if (bySource.session.length) {
    sectionLines.push("=== Prior session turns ===");
    for (const e of bySource.session) {
      sectionLines.push(formatEntry(e));
      sectionLines.push("");
    }
  }

  let fullContext: string;
  if (total > 0) {
    fullContext =
      `${header}\n\nRelevant context from this session's memory:\n\n` +
      sectionLines.join("\n").trim();
    hookLog("context_lookup_hit", { counts, saves_last_turn: savesLastTurn });
  } else {
    fullContext = `${header}\n\n(no memory matches for this prompt)`;
    hookLog("context_lookup_empty", { saves_last_turn: savesLastTurn });
  }

  // Audit log: persist full recall details per turn (best-effort).
  try {
    const auditPath = join(pluginStateDir(), "recall-audit.log");
    mkdirSync(pluginStateDir(), { recursive: true });
    writeFileSync(
      auditPath,
      JSON.stringify({
        ts: new Date().toISOString(),
        session_id: sessionId,
        prompt,
        hits: counts,
        context: fullContext,
      }) + "\n",
      { flag: "a", encoding: "utf-8" },
    );
  } catch (err) {
    hookLog("recall_audit_write_failed", { error: String(err).slice(0, 200) });
  }

  return fullContext;
}

/**
 * Resolve the session ID from the current env session key.
 * Used by hooks that don't have the conversationId directly.
 */
export function resolveSessionFromEnv(): string {
  const sessionKey = getSessionKey();
  if (!sessionKey) return "";
  const cfg = loadConfig();
  return resolveSessionId(sessionKey, cfg.agentName);
}

export default searchContext;
