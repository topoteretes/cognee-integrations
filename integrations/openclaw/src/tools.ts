// ---------------------------------------------------------------------------
// Agent tools: memory_search / memory_get
//
// OpenClaw's memory slot comes with a tool contract. The bundled memory-core
// plugin registers `memory_search` and `memory_get`, and the always-on
// `active-memory` extension runs a sub-agent before conversational replies
// with exactly those two tools allow-listed. When Cognee owns the slot and
// registers nothing, that sub-agent fails with "No callable tools remain…"
// even though Cognee itself is healthy.
//
// These tools fill the contract with Cognee-backed implementations:
//
//   memory_search  — recall across the configured scopes (and, with
//                    corpus=sessions/all, the live session cache). Returns
//                    `{ results: [...] }`; `{ results: [], disabled: true }`
//                    when memory is unavailable, which is the signal
//                    active-memory looks for.
//   memory_get     — resolve a `reference` handed out by memory_search back to
//                    its full text (provenance included), or read a bounded
//                    excerpt of a workspace memory file (MEMORY.md, memory/*.md)
//                    the way memory-core does. Stale references return a
//                    structured not-found result, never a throw.
//
// Both are independent of `autoRecall`: prompt-time injection and
// model-invoked search are different rails.
// ---------------------------------------------------------------------------

import { promises as fs } from "node:fs";
import { isAbsolute, join, normalize, relative, sep } from "node:path";
import type { CogneeHttpClient } from "./client.js";
import type { CogneePluginConfig, CogneeSearchResult } from "./types.js";

// Tool result shape the OpenClaw agent loop expects (AgentToolResult): text
// content for the model plus structured `details` for logs/UI. Kept local so
// the plugin doesn't import runtime code from the SDK (only types), which keeps
// the jest harness free of ESM-only SDK modules.
export type ToolResult<T> = {
  content: Array<{ type: "text"; text: string }>;
  details: T;
};

export function jsonResult<T>(payload: T): ToolResult<T> {
  return { content: [{ type: "text", text: JSON.stringify(payload, null, 2) }], details: payload };
}

// ---------------------------------------------------------------------------
// Schemas (JSON Schema; mirrors memory-core's contract so active-memory and
// operator allowlists work unchanged)
// ---------------------------------------------------------------------------

export const MEMORY_SEARCH_CORPORA = ["memory", "sessions", "all", "wiki"] as const;
export type MemorySearchCorpus = (typeof MEMORY_SEARCH_CORPORA)[number];

export const MemorySearchSchema = {
  type: "object",
  properties: {
    query: { type: "string", description: "Natural-language question or topic to recall." },
    maxResults: { type: "integer", minimum: 1, description: "Cap on returned results (default: plugin maxResults)." },
    minScore: { type: "number", description: "Drop results scoring below this (default: plugin minScore)." },
    corpus: {
      type: "string",
      enum: [...MEMORY_SEARCH_CORPORA],
      description: "memory = permanent knowledge graph; sessions = this conversation's session cache; all = both (default). wiki is not backed by Cognee and returns no results.",
    },
  },
  required: ["query"],
  additionalProperties: false,
} as const;

export const MemoryGetSchema = {
  type: "object",
  properties: {
    path: { type: "string", description: "A `reference` from memory_search, or a workspace memory file path such as MEMORY.md or memory/notes.md." },
    from: { type: "integer", minimum: 1, description: "1-based first line (file paths only)." },
    lines: { type: "integer", minimum: 1, description: "Number of lines to return (file paths only; default 80)." },
    corpus: { type: "string", enum: ["memory", "wiki", "all"] },
  },
  required: ["path"],
  additionalProperties: false,
} as const;

export type MemorySearchParams = {
  query: string;
  maxResults?: number;
  minScore?: number;
  corpus?: MemorySearchCorpus;
};

export type MemoryGetParams = {
  path: string;
  from?: number;
  lines?: number;
  corpus?: "memory" | "wiki" | "all";
};

// ---------------------------------------------------------------------------
// Result shapes
// ---------------------------------------------------------------------------

export type MemorySearchHit = {
  /** Opaque handle for memory_get: `cognee://<scope>/<id>`. */
  reference: string;
  text: string;
  score: number;
  /** Which corpus produced the hit: "graph" (permanent memory) or "session". */
  scope: "graph" | "session";
  /** Best-effort provenance label (file name, dataset, or scope). */
  source: string;
  /** ISO timestamp when the server supplied one. */
  time?: string;
  // memory-core-compatible aliases so generic consumers that read
  // `path`/`snippet` keep working.
  path: string;
  snippet: string;
};

export type MemorySearchResult = {
  results: MemorySearchHit[];
  query: string;
  corpus: MemorySearchCorpus;
  /** Set (with `error`) when memory could not be searched at all. */
  disabled?: true;
  unavailable?: true;
  error?: string;
  warning?: string;
  action?: string;
  note?: string;
};

export type MemoryGetResult = {
  path: string;
  text: string;
  /** Provenance for references; file metadata for workspace files. */
  source?: string;
  scope?: "graph" | "session" | "file";
  score?: number;
  time?: string;
  from?: number;
  lines?: number;
  totalLines?: number;
  truncated?: boolean;
  nextFrom?: number;
  disabled?: true;
  error?: string;
};

// ---------------------------------------------------------------------------
// Reference handles + a small cache so memory_get is a lookup, not a search
// ---------------------------------------------------------------------------

export const REFERENCE_PREFIX = "cognee://";
const REFERENCE_CACHE_MAX = 500;

export function makeReference(scope: "graph" | "session", id: string): string {
  return `${REFERENCE_PREFIX}${scope}/${encodeURIComponent(id)}`;
}

export function parseReference(value: string): { scope: "graph" | "session"; id: string } | null {
  if (!value.startsWith(REFERENCE_PREFIX)) return null;
  const rest = value.slice(REFERENCE_PREFIX.length);
  const slash = rest.indexOf("/");
  if (slash <= 0) return null;
  const scope = rest.slice(0, slash);
  if (scope !== "graph" && scope !== "session") return null;
  const id = decodeURIComponent(rest.slice(slash + 1));
  return id ? { scope, id } : null;
}

/** Bounded insertion-ordered cache of hits handed to the model. */
export class ReferenceCache {
  private readonly map = new Map<string, MemorySearchHit>();
  constructor(private readonly max = REFERENCE_CACHE_MAX) {}

  put(hit: MemorySearchHit): void {
    this.map.delete(hit.reference);
    this.map.set(hit.reference, hit);
    while (this.map.size > this.max) {
      const oldest = this.map.keys().next().value;
      if (oldest === undefined) break;
      this.map.delete(oldest);
    }
  }

  get(reference: string): MemorySearchHit | undefined {
    return this.map.get(reference);
  }

  get size(): number {
    return this.map.size;
  }
}

// ---------------------------------------------------------------------------
// Provenance helpers
// ---------------------------------------------------------------------------

const SOURCE_KEYS = ["source", "file_path", "filePath", "name", "document_name", "origin", "type"] as const;
const TIME_KEYS = ["created_at", "createdAt", "timestamp", "time", "updated_at", "updatedAt"] as const;

export function hitSource(result: CogneeSearchResult, fallback: string): string {
  const meta = result.metadata;
  if (meta && typeof meta === "object") {
    for (const key of SOURCE_KEYS) {
      const v = meta[key];
      if (typeof v === "string" && v.trim()) return v.trim().split("/").pop()!.replace(/\.txt$/, "");
    }
  }
  return fallback;
}

export function hitTime(result: CogneeSearchResult): string | undefined {
  const meta = result.metadata;
  if (!meta || typeof meta !== "object") return undefined;
  for (const key of TIME_KEYS) {
    const v = meta[key];
    if (typeof v === "string" && v.trim()) return v;
    if (typeof v === "number" && Number.isFinite(v)) return new Date(v > 1e12 ? v : v * 1000).toISOString();
  }
  return undefined;
}

export function toHit(result: CogneeSearchResult, scope: "graph" | "session", fallbackSource: string): MemorySearchHit {
  const text = typeof result.text === "string" ? result.text : String(result.text ?? "");
  const time = hitTime(result);
  return {
    reference: makeReference(scope, result.id),
    text,
    score: typeof result.score === "number" ? result.score : 0,
    scope,
    source: hitSource(result, fallbackSource),
    ...(time ? { time } : {}),
    path: makeReference(scope, result.id),
    snippet: text.length > 400 ? `${text.slice(0, 400)}…` : text,
  };
}

// ---------------------------------------------------------------------------
// Workspace memory file reads (memory_get on a path)
// ---------------------------------------------------------------------------

const DEFAULT_GET_LINES = 80;

/** True for the file paths memory-core's memory_get accepts: MEMORY.md and memory/**. */
export function isMemoryFilePath(p: string): boolean {
  const n = normalize(p).replace(/\\/g, "/").replace(/^\.\//, "");
  if (!n || n.startsWith("../") || n === ".." || isAbsolute(n)) return false;
  return n === "MEMORY.md" || n.startsWith("memory/");
}

export async function readMemoryFileExcerpt(
  workspaceDir: string,
  relPath: string,
  from?: number,
  lines?: number,
): Promise<MemoryGetResult> {
  const abs = join(workspaceDir, relPath);
  // Refuse anything that normalizes outside the workspace.
  const rel = relative(workspaceDir, abs);
  if (rel.startsWith("..") || rel.split(sep).includes("..")) {
    return { path: relPath, text: "", error: "path escapes the workspace" };
  }
  let raw: string;
  try {
    raw = await fs.readFile(abs, "utf-8");
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT") return { path: relPath, text: "", error: "file not found" };
    return { path: relPath, text: "", error: `read failed: ${String(e)}` };
  }
  const all = raw.split("\n");
  const start = Math.max(1, from ?? 1);
  const count = Math.max(1, lines ?? DEFAULT_GET_LINES);
  const slice = all.slice(start - 1, start - 1 + count);
  const end = start - 1 + slice.length;
  const truncated = end < all.length;
  return {
    path: relPath,
    text: slice.join("\n"),
    scope: "file",
    source: relPath,
    from: start,
    lines: slice.length,
    totalLines: all.length,
    truncated,
    ...(truncated ? { nextFrom: end + 1 } : {}),
  };
}

// ---------------------------------------------------------------------------
// Tool construction
// ---------------------------------------------------------------------------

export type RecallFn = (params: {
  queryText: string;
  searchType: CogneePluginConfig["searchType"];
  datasetIds: string[];
  searchPrompt?: string;
  topK?: number;
  sessionId?: string;
  scope?: string | string[];
  contextProfile?: "qa" | "agent";
}) => Promise<CogneeSearchResult[]>;

/**
 * Session-cache layers recalled alongside the graph. The server's default
 * scope ("auto") is graph-only whenever dataset_ids/search_type are supplied,
 * so these must be requested explicitly.
 */
export const SESSION_LAYER_SCOPES = ["session", "trace", "session_context"] as const;

export type MemoryToolsDeps = {
  cfg: Required<CogneePluginConfig>;
  /** Dataset ids to search for this agent/conversation, plus scope labels for provenance. */
  resolveDatasets: (agentId?: string, ctx?: MemoryToolContext) => Promise<Array<{ id: string; label: string }>>;
  /** Recall call (breaker-aware in production). */
  recall: RecallFn;
  /** Seconds until the recall breaker closes; 0 when closed. */
  breakerOpenForSeconds?: () => Promise<number>;
  /** Cognee session id for corpus=sessions (conversation-aware). */
  sessionIdFor?: (hostSessionId?: string, ctx?: MemoryToolContext) => string | undefined;
  cache?: ReferenceCache;
  logger?: { debug?: (m: string) => void; warn?: (m: string) => void };
};

export type MemoryToolContext = {
  agentId?: string;
  sessionId?: string;
  sessionKey?: string;
  workspaceDir?: string;
};

export type MemoryTool<P, R> = {
  name: string;
  label: string;
  description: string;
  parameters: Record<string, unknown>;
  execute: (toolCallId: string, params: P) => Promise<ToolResult<R>>;
};

function unavailable(query: string, corpus: MemorySearchCorpus, error: string, action: string): MemorySearchResult {
  return {
    results: [],
    query,
    corpus,
    disabled: true,
    unavailable: true,
    error,
    warning: "Cognee memory is unavailable right now.",
    action,
  };
}

export function createMemorySearchTool(deps: MemoryToolsDeps, ctx: MemoryToolContext): MemoryTool<MemorySearchParams, MemorySearchResult> {
  const cache = deps.cache ?? new ReferenceCache();
  return {
    name: "memory_search",
    label: "Memory Search",
    description:
      "Mandatory recall step: search Cognee long-term memory (knowledge graph built from MEMORY.md, memory/*.md and past sessions) before answering questions about prior work, decisions, dates, people, preferences, or todos. " +
      "`corpus=memory` searches the permanent graph, `corpus=sessions` this conversation's session cache, `corpus=all` (default) both. " +
      "Each result carries a `reference` you can pass to memory_get for the full text. If the response has disabled=true, memory is unavailable — tell the user and include the warning/action guidance.",
    parameters: MemorySearchSchema as unknown as Record<string, unknown>,
    async execute(_id, params) {
      const query = typeof params?.query === "string" ? params.query.trim() : "";
      const corpus: MemorySearchCorpus = params?.corpus && (MEMORY_SEARCH_CORPORA as readonly string[]).includes(params.corpus) ? params.corpus : "all";
      if (!query) return jsonResult<MemorySearchResult>({ results: [], query, corpus, error: "query is required" });

      if (corpus === "wiki") {
        return jsonResult<MemorySearchResult>({ results: [], query, corpus, note: "Cognee does not serve a wiki corpus; use corpus=memory or corpus=all." });
      }

      const retryIn = deps.breakerOpenForSeconds ? await deps.breakerOpenForSeconds() : 0;
      if (retryIn > 0) {
        return jsonResult(unavailable(query, corpus, `recall breaker open (retry in ${Math.ceil(retryIn)}s)`, "Wait for the Cognee server to recover, then retry memory_search."));
      }

      const topK = typeof params.maxResults === "number" && params.maxResults >= 1 ? Math.floor(params.maxResults) : deps.cfg.maxResults;
      const minScore = typeof params.minScore === "number" ? params.minScore : deps.cfg.minScore;

      let datasets: Array<{ id: string; label: string }>;
      try {
        datasets = await deps.resolveDatasets(ctx.agentId, ctx);
      } catch (e) {
        return jsonResult(unavailable(query, corpus, `dataset resolution failed: ${String(e)}`, "Check the Cognee server connection and retry memory_search."));
      }
      if (datasets.length === 0) {
        return jsonResult<MemorySearchResult>({ results: [], query, corpus, note: "No Cognee dataset is indexed yet for this agent. Run `openclaw cognee index` or let auto-index finish." });
      }

      const wantGraph = corpus === "memory" || corpus === "all";
      const wantSession = corpus === "sessions" || corpus === "all";
      const sessionId = wantSession ? deps.sessionIdFor?.(ctx.sessionId, ctx) : undefined;

      const tasks: Array<Promise<MemorySearchHit[]>> = [];
      if (wantGraph) {
        for (const ds of datasets) {
          tasks.push(
            deps.recall({ queryText: query, searchType: deps.cfg.searchType, datasetIds: [ds.id], searchPrompt: deps.cfg.searchPrompt, topK })
              .then((rs) => rs.map((r) => toHit(r, "graph", ds.label))),
          );
        }
      }
      if (wantSession && sessionId) {
        tasks.push(
          deps.recall({
            queryText: query,
            searchType: deps.cfg.searchType,
            datasetIds: datasets.map((d) => d.id),
            searchPrompt: deps.cfg.searchPrompt,
            topK,
            sessionId,
            scope: [...SESSION_LAYER_SCOPES],
            contextProfile: "agent",
          }).then((rs) => rs.map((r) => toHit(r, "session", r.source === "session_context" ? "agent guidance" : r.source === "trace" ? "trace" : "session"))),
        );
      }

      const settled = await Promise.allSettled(tasks);
      const hits: MemorySearchHit[] = [];
      const errors: string[] = [];
      for (const s of settled) {
        if (s.status === "fulfilled") hits.push(...s.value);
        else errors.push(String(s.reason));
      }
      if (hits.length === 0 && errors.length > 0 && errors.length === settled.length) {
        deps.logger?.warn?.(`cognee-openclaw: memory_search failed: ${errors[0]}`);
        return jsonResult(unavailable(query, corpus, errors[0], "Check the Cognee server (openclaw cognee health) and retry memory_search."));
      }

      // Dedupe by reference, filter, rank, cap.
      const seen = new Set<string>();
      const results = hits
        .filter((h) => h.score >= minScore)
        .filter((h) => (seen.has(h.reference) ? false : (seen.add(h.reference), true)))
        .sort((a, b) => b.score - a.score)
        .slice(0, topK);
      for (const h of results) cache.put(h);

      const out: MemorySearchResult = { results, query, corpus };
      if (errors.length > 0) out.warning = `Some scopes failed: ${errors.join("; ").slice(0, 300)}`;
      deps.logger?.debug?.(`cognee-openclaw: memory_search "${query.slice(0, 60)}" -> ${results.length} result(s)`);
      return jsonResult(out);
    },
  };
}

export function createMemoryGetTool(deps: MemoryToolsDeps, ctx: MemoryToolContext): MemoryTool<MemoryGetParams, MemoryGetResult> {
  const cache = deps.cache ?? new ReferenceCache();
  return {
    name: "memory_get",
    label: "Memory Get",
    description:
      "Resolve a `reference` returned by memory_search to its full stored text with provenance, or read a bounded excerpt of a workspace memory file (MEMORY.md, memory/*.md) with optional `from`/`lines`. " +
      "A stale or unknown reference returns an error field, not a failure — run memory_search again to obtain fresh references.",
    parameters: MemoryGetSchema as unknown as Record<string, unknown>,
    async execute(_id, params) {
      const path = typeof params?.path === "string" ? params.path.trim() : "";
      if (!path) return jsonResult<MemoryGetResult>({ path, text: "", error: "path is required" });

      if (params.corpus === "wiki") {
        return jsonResult<MemoryGetResult>({ path, text: "", disabled: true, error: "wiki corpus is not backed by Cognee" });
      }

      const ref = parseReference(path);
      if (ref) {
        const hit = cache.get(path);
        if (!hit) {
          return jsonResult<MemoryGetResult>({ path, text: "", error: "reference not found (stale or from another session) — run memory_search again" });
        }
        return jsonResult<MemoryGetResult>({
          path,
          text: hit.text,
          source: hit.source,
          scope: hit.scope,
          score: hit.score,
          ...(hit.time ? { time: hit.time } : {}),
        });
      }

      if (isMemoryFilePath(path)) {
        if (!ctx.workspaceDir) return jsonResult<MemoryGetResult>({ path, text: "", error: "no workspace directory available for file reads" });
        return jsonResult(await readMemoryFileExcerpt(ctx.workspaceDir, path, params.from, params.lines));
      }

      return jsonResult<MemoryGetResult>({
        path,
        text: "",
        error: "path must be a cognee:// reference from memory_search or a workspace memory file (MEMORY.md, memory/...)",
      });
    },
  };
}

/** Build both tools sharing one reference cache. */
export function createMemoryTools(deps: MemoryToolsDeps, ctx: MemoryToolContext): [MemoryTool<MemorySearchParams, MemorySearchResult>, MemoryTool<MemoryGetParams, MemoryGetResult>] {
  const shared: MemoryToolsDeps = { ...deps, cache: deps.cache ?? new ReferenceCache() };
  return [createMemorySearchTool(shared, ctx), createMemoryGetTool(shared, ctx)];
}
