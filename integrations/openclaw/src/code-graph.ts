// ---------------------------------------------------------------------------
// Code graph (enola pipeline) — the operator-driven subset
//
// The claude-code/codex plugins index the repo an agent is launched in
// automatically, re-index on every changed turn, and add a deterministic
// "code" recall lane. OpenClaw agents are rarely launched inside a checkout,
// so this port keeps the pieces that make the code graph *available* without
// assuming a coding workflow:
//
//   * `openclaw cognee index-repo <path|url>` submits a repository to the
//     server-side pipeline (POST /remember, content_type="code" — no LLM or
//     embedding calls) into one narrow dataset per repo and records it in
//     ~/.openclaw/memory/cognee/code-graphs.json.
//   * `memory_code_search` lets the model run exact structural queries
//     (callers, impact, paths, endpoints) against a registered code graph.
//   * An additive recall lane fires only when a prompt carries an
//     identifier-shaped token AND at least one code graph is registered (or
//     listed in `codeDatasets`), so conversational agents never pay for it.
//
// Not ported (on purpose): session-start autoindex and the per-turn git
// fingerprint re-ingest. Freshness is the operator's: re-run index-repo.
// Requires a Cognee server >= 1.5.3.
// ---------------------------------------------------------------------------

import { createHash } from "node:crypto";
import { realpathSync } from "node:fs";
import { basename, resolve } from "node:path";
import type { CodeGraphRecord, CodeGraphsFile, CogneePluginConfig, CogneeSearchResult } from "./types.js";
import { CODE_GRAPHS_PATH, loadCodeGraphs, saveCodeGraphs } from "./persistence.js";
import { jsonResult, type MemoryTool, type MemoryToolContext } from "./tools.js";

// ---------------------------------------------------------------------------
// Repository specs and dataset naming (mirrors _code_graph.py)
// ---------------------------------------------------------------------------

export function isRemoteRepo(spec: string): boolean {
  return /^(https?:\/\/|git@|ssh:\/\/)/.test(spec.trim());
}

/** Stable identity: realpath for local checkouts, trimmed URL for remotes. */
export function canonicalSpec(spec: string): string {
  const s = spec.trim();
  if (isRemoteRepo(s)) {
    let c = s.replace(/\/+$/, "");
    if (c.endsWith(".git")) c = c.slice(0, -4);
    return c;
  }
  try {
    return realpathSync(resolve(s));
  } catch {
    return resolve(s);
  }
}

function readableTail(canonical: string): string {
  const tail = basename(canonical.replace(/\/+$/, "")) || "repo";
  return tail.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[-.]+|[-.]+$/g, "") || "repo";
}

/** `codebase-<repo>-<8-hex digest of the canonical spec>` — unique per checkout, readable in listings. */
export function defaultCodeDataset(spec: string): string {
  const canonical = canonicalSpec(spec);
  const digest = createHash("sha256").update(canonical, "utf-8").digest("hex").slice(0, 8);
  return `codebase-${readableTail(canonical).toLowerCase()}-${digest}`;
}

// ---------------------------------------------------------------------------
// Identifier gate for the recall lane (mirrors extract_identifiers)
// ---------------------------------------------------------------------------

const CODE_EXTENSIONS = [
  "c", "cc", "cpp", "cs", "css", "dart", "el", "ex", "exs", "go", "h", "hpp", "html", "java", "js", "jsx", "kt", "kts",
  "lua", "m", "md", "mjs", "php", "pl", "py", "r", "rb", "rs", "scala", "scss", "sh", "sql", "swift", "tf", "toml", "ts",
  "tsx", "vb", "vue", "yaml", "yml", "zig",
];

const CAMEL_STOPLIST = new Set([
  "Claude", "ClaudeCode", "Codex", "Cognee", "GitHub", "GitLab", "JavaScript", "TypeScript", "PostgreSQL", "MongoDB",
  "OpenAI", "OpenClaw", "MacOS", "ReadMe", "WiFi", "OAuth", "TODOs", "Telegram", "WhatsApp", "YouTube", "LinkedIn",
]);

const BACKTICK_RE = /`([^`\n]{2,120})`/g;
const FILEPATH_RE = new RegExp(String.raw`\b[\w./-]{1,120}\.(?:${CODE_EXTENSIONS.join("|")})\b`, "g");
const DOTTED_RE = /\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b/g;
const SNAKE_RE = /\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g;
const CAMEL_RE = /\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b/g;
const IDENTIFIER_CHARS_RE = /^[\w./:-]+$/;
const TLDS = new Set(["com", "org", "net", "io", "ai", "dev"]);

/**
 * Identifier-shaped tokens from a prompt, best first, at most `limit`. Empty
 * means the syntactic gate did not fire and the code lane must stay off.
 * Conservative on purpose: it keeps the lane OFF conversational prompts.
 */
export function extractIdentifiers(prompt: string, limit = 2): string[] {
  if (!prompt) return [];
  const found: string[] = [];
  const seen = new Set<string>();
  const add = (raw: string) => {
    const token = raw.trim().replace(/^[.,:;()[\]{}]+|[.,:;()[\]{}]+$/g, "");
    if (token.length < 3 || token.length > 120) return;
    if (CAMEL_STOPLIST.has(token)) return;
    const key = token.toLowerCase();
    if (seen.has(key)) return;
    // "plugin.ts" after "src/plugin.ts" is the same symbol, not a second one.
    if (found.some((f) => f.toLowerCase().endsWith(`/${key}`) || f.toLowerCase().endsWith(`.${key}`))) return;
    seen.add(key);
    found.push(token);
  };

  for (const m of prompt.matchAll(BACKTICK_RE)) {
    const inner = m[1].trim();
    if (!inner.includes(" ") && IDENTIFIER_CHARS_RE.test(inner)) add(inner);
  }
  for (const m of prompt.matchAll(FILEPATH_RE)) add(m[0]);
  for (const m of prompt.matchAll(DOTTED_RE)) {
    const token = m[0];
    const parts = token.split(".");
    if (token.length < 5 || !parts.some((p) => p.length >= 3)) continue;
    if (parts.length === 2 && TLDS.has(parts[1].toLowerCase())) continue;
    add(token);
  }
  for (const m of prompt.matchAll(SNAKE_RE)) add(m[0]);
  for (const m of prompt.matchAll(CAMEL_RE)) add(m[0]);
  return found.slice(0, limit);
}

/** The auto-recall code_query: a bounded substring fact lookup that degrades to an empty page. */
export function buildCodeQuery(identifier: string, limit = 5): Record<string, unknown> {
  return { operation: "query_facts", name: identifier, limit };
}

// ---------------------------------------------------------------------------
// Registry of indexed repositories
// ---------------------------------------------------------------------------

const sharedRegistries = new Map<string, CodeGraphRegistry>();

export class CodeGraphRegistry {
  /** One registry per file within this process (see DatasetSwitchStore.shared). */
  static shared(opts: ConstructorParameters<typeof CodeGraphRegistry>[0] = {}): CodeGraphRegistry {
    const key = opts.path ?? CODE_GRAPHS_PATH;
    let reg = sharedRegistries.get(key);
    if (!reg) {
      reg = new CodeGraphRegistry(opts);
      sharedRegistries.set(key, reg);
    }
    return reg;
  }

  /** Drop the shared instances (tests). */
  static resetShared(): void {
    sharedRegistries.clear();
  }

  private data: CodeGraphsFile = {};
  private readonly loaded: Promise<void>;
  private chain: Promise<void> = Promise.resolve();

  constructor(private readonly opts: { path?: string; now?: () => Date; warn?: (m: string) => void; debug?: (m: string) => void } = {}) {
    this.loaded = Promise.resolve()
      .then(() => loadCodeGraphs(opts.path))
      .then((d) => { this.data = { ...d, ...this.data }; })
      .catch((e) => this.opts.debug?.(`cognee-openclaw: code-graph registry load skipped: ${String(e)}`));
  }

  ready(): Promise<void> { return this.loaded; }
  flush(): Promise<void> { return this.chain; }

  list(): CodeGraphRecord[] {
    return Object.values(this.data).sort((a, b) => (b.indexedAt ?? "").localeCompare(a.indexedAt ?? ""));
  }

  get(dataset: string): CodeGraphRecord | undefined {
    return this.data[dataset];
  }

  upsert(record: Omit<CodeGraphRecord, "indexedAt"> & { indexedAt?: string }): CodeGraphRecord {
    const full: CodeGraphRecord = { ...record, indexedAt: record.indexedAt ?? (this.opts.now ?? (() => new Date()))().toISOString() };
    this.data[full.dataset] = full;
    this.persist();
    return full;
  }

  remove(dataset: string): boolean {
    if (!this.data[dataset]) return false;
    delete this.data[dataset];
    this.persist();
    return true;
  }

  private persist(): void {
    this.chain = this.chain
      .then(() => this.loaded)
      .then(() => saveCodeGraphs(this.data, this.opts.path))
      .catch((e) => this.opts.warn?.(`cognee-openclaw: code-graph registry save failed: ${String(e)}`));
  }
}

// ---------------------------------------------------------------------------
// Prompt section for the recall lane
// ---------------------------------------------------------------------------

export const MAX_CODE_FACTS = 8;
const MAX_FACT_CHARS = 600;

/** `<code_graph>` block from code-scope recall results; [] when nothing usable. */
export function renderCodeGraphSection(results: readonly CogneeSearchResult[], identifier: string, dataset: string): string[] {
  const facts = results
    .filter((r) => (r.source ?? "code") === "code")
    .map((r) => (r.text ?? "").trim())
    .filter(Boolean)
    .slice(0, MAX_CODE_FACTS)
    .map((t) => (t.length > MAX_FACT_CHARS ? `${t.slice(0, MAX_FACT_CHARS)}…` : t));
  if (facts.length === 0) return [];
  return [
    `<code_graph>\n[Deterministic code-graph facts for "${identifier}" from dataset ${dataset}; exact, not semantic]\n${facts.map((f) => `- ${f.replace(/\n/g, "\n  ")}`).join("\n")}\n</code_graph>`,
  ];
}

// ---------------------------------------------------------------------------
// Tool: memory_code_search
// ---------------------------------------------------------------------------

export const CODE_OPERATIONS = ["query_facts", "explore", "traverse", "find_path", "impact_analysis", "delta"] as const;
export type CodeOperation = (typeof CODE_OPERATIONS)[number];

export const MemoryCodeSearchSchema = {
  type: "object",
  properties: {
    query: {
      type: "string",
      description: "Seed symbol, file or endpoint name (exact/suffix/substring match). For find_path pass `source` and `target` in args instead; for delta the query is ignored.",
    },
    operation: {
      type: "string",
      enum: [...CODE_OPERATIONS],
      description: "query_facts = filtered listing (default); explore = one node's neighborhood; traverse = follow edges from the seed; find_path = how source reaches target; impact_analysis = what breaks if the seed changes; delta = what the last index changed.",
    },
    args: {
      type: "object",
      description: "Operation-specific arguments merged into the query, e.g. {\"kind\":\"route\"} for query_facts, {\"max_depth\":2} for explore/traverse, {\"direction\":\"forward\"}, {\"source\":\"A\",\"target\":\"B\"} for find_path, {\"targets\":[\"fn\"]} for impact_analysis.",
      additionalProperties: true,
    },
    dataset: { type: "string", description: "Code-graph dataset to query. Optional when exactly one repository is indexed." },
    limit: { type: "integer", minimum: 1, maximum: 50, description: "Cap on returned facts (default 10)." },
  },
  required: [],
  additionalProperties: false,
} as const;

export type MemoryCodeSearchParams = {
  query?: string;
  operation?: CodeOperation;
  args?: Record<string, unknown>;
  dataset?: string;
  limit?: number;
};

export type MemoryCodeSearchResult = {
  results: Array<{ text: string; raw?: unknown }>;
  dataset?: string;
  operation: CodeOperation;
  codeQuery: Record<string, unknown>;
  availableDatasets?: string[];
  disabled?: true;
  error?: string;
  note?: string;
};

export type CodeSearchDeps = {
  cfg: Required<CogneePluginConfig>;
  /** Registered + configured code datasets, most recently indexed first (awaits the registry load). */
  codeDatasets: () => Promise<string[]> | string[];
  resolveDatasetId: (name: string) => Promise<string | undefined>;
  recall: (params: {
    queryText: string;
    datasetIds: string[];
    scope: string[];
    codeQuery: Record<string, unknown>;
    topK?: number;
  }) => Promise<CogneeSearchResult[]>;
  logger?: { debug?: (m: string) => void; warn?: (m: string) => void };
};

/** Build the server code_query from tool params. */
export function buildToolCodeQuery(params: MemoryCodeSearchParams): { operation: CodeOperation; codeQuery: Record<string, unknown>; error?: string } {
  const operation: CodeOperation = params.operation && (CODE_OPERATIONS as readonly string[]).includes(params.operation) ? params.operation : "query_facts";
  const args = params.args && typeof params.args === "object" ? { ...params.args } : {};
  const seed = typeof params.query === "string" ? params.query.trim() : "";
  const limit = typeof params.limit === "number" && params.limit >= 1 ? Math.min(50, Math.floor(params.limit)) : 10;
  const q: Record<string, unknown> = { operation, ...args };
  switch (operation) {
    case "query_facts":
      if (seed && q.name === undefined) q.name = seed;
      if (q.limit === undefined) q.limit = limit;
      break;
    case "explore":
    case "impact_analysis":
    case "traverse":
      if (operation === "explore" && q.name === undefined && seed) q.name = seed;
      if (operation === "traverse" && q.start === undefined && seed) q.start = seed;
      if (operation === "impact_analysis" && q.targets === undefined && seed) q.targets = [seed];
      if (!seed && q.name === undefined && q.start === undefined && q.targets === undefined) return { operation, codeQuery: q, error: `${operation} needs a seed: pass query` };
      break;
    case "find_path":
      if (q.source === undefined || q.target === undefined) return { operation, codeQuery: q, error: "find_path needs args.source and args.target" };
      break;
    case "delta":
      break;
  }
  return { operation, codeQuery: q };
}

export function createMemoryCodeSearchTool(deps: CodeSearchDeps, _ctx: MemoryToolContext): MemoryTool<MemoryCodeSearchParams, MemoryCodeSearchResult> {
  return {
    name: "memory_code_search",
    label: "Memory Code Search",
    description:
      "Query an indexed repository's code graph deterministically (no LLM): who calls X, what breaks if X changes, how A reaches B, all routes/endpoints, what the last index changed. " +
      "Use for structural questions that name a symbol, file or endpoint; use memory_search for conceptual questions. Repositories are indexed by the operator with `openclaw cognee index-repo`; pass `dataset` when more than one is indexed.",
    parameters: MemoryCodeSearchSchema as unknown as Record<string, unknown>,
    async execute(_id, params) {
      const { operation, codeQuery, error } = buildToolCodeQuery(params ?? {});
      const available = await deps.codeDatasets();
      if (error) return jsonResult<MemoryCodeSearchResult>({ results: [], operation, codeQuery, availableDatasets: available, error });

      const requested = typeof params?.dataset === "string" ? params.dataset.trim() : "";
      const dataset = requested || (available.length === 1 ? available[0] : "");
      if (!dataset) {
        return jsonResult<MemoryCodeSearchResult>({
          results: [],
          operation,
          codeQuery,
          availableDatasets: available,
          error: available.length === 0
            ? "No code graph is indexed. Ask the operator to run `openclaw cognee index-repo <path|url>`."
            : "Several code graphs are indexed — pass `dataset` (see availableDatasets).",
        });
      }

      let datasetId: string | undefined;
      try {
        datasetId = await deps.resolveDatasetId(dataset);
      } catch (e) {
        return jsonResult<MemoryCodeSearchResult>({ results: [], dataset, operation, codeQuery, disabled: true, error: `dataset resolution failed: ${String(e)}` });
      }
      if (!datasetId) {
        return jsonResult<MemoryCodeSearchResult>({ results: [], dataset, operation, codeQuery, availableDatasets: available, error: `dataset "${dataset}" not found on the server — was it indexed?` });
      }

      const seed = typeof params?.query === "string" && params.query.trim() ? params.query.trim() : operation;
      try {
        const results = await deps.recall({ queryText: seed, datasetIds: [datasetId], scope: ["code"], codeQuery, topK: typeof codeQuery.limit === "number" ? codeQuery.limit : 10 });
        const out = results
          .filter((r) => (r.source ?? "code") === "code")
          .map((r) => ({ text: r.text, ...(r.metadata ? { raw: r.metadata } : {}) }));
        deps.logger?.debug?.(`cognee-openclaw: memory_code_search ${operation} on ${dataset} -> ${out.length} fact(s)`);
        return jsonResult<MemoryCodeSearchResult>({
          results: out,
          dataset,
          operation,
          codeQuery,
          ...(out.length === 0 ? { note: "No facts matched. An unresolvable seed returns empty, not an error — check the exact symbol name, or use query_facts for a substring listing." } : {}),
        });
      } catch (e) {
        const msg = String(e);
        deps.logger?.warn?.(`cognee-openclaw: memory_code_search failed: ${msg}`);
        // The server returns a structured error naming candidates on an ambiguous seed.
        return jsonResult<MemoryCodeSearchResult>({ results: [], dataset, operation, codeQuery, ...(/\(4\d\d\)/.test(msg) ? {} : { disabled: true as const }), error: msg });
      }
    },
  };
}
