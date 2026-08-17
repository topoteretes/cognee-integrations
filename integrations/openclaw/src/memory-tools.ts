import { createHash } from "node:crypto";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk";

export type MemoryToolHit = {
  id: string;
  text: string;
  score: number;
  scope: string;
  source?: string;
  time?: string;
  datasetName: string;
  datasetId: string;
};

type ToolContext = { agentId?: string; sessionId?: string; sessionKey?: string };
type SearchOptions = ToolContext & { maxResults?: number; minScore?: number; signal?: AbortSignal };

const searchSchema = {
  type: "object",
  properties: {
    query: { type: "string" },
    maxResults: { type: "integer", minimum: 1 },
    minScore: { type: "number" },
    corpus: { type: "string", enum: ["memory", "wiki", "all", "sessions"] },
  },
  required: ["query"],
  additionalProperties: false,
} as const;

const getSchema = {
  type: "object",
  properties: {
    path: { type: "string" },
    from: { type: "integer", minimum: 1 },
    lines: { type: "integer", minimum: 1 },
    corpus: { type: "string", enum: ["memory", "wiki", "all"] },
  },
  required: ["path"],
  additionalProperties: false,
} as const;

function jsonResult(payload: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(payload, null, 2) }], details: payload };
}

function ownerKey(ctx: ToolContext): string | undefined {
  const session = ctx.sessionId ?? ctx.sessionKey;
  return session ? `${ctx.agentId ?? "default"}::${session}` : undefined;
}

function referenceFor(hit: MemoryToolHit, owner: string): string {
  const digest = createHash("sha256")
    .update(`${owner}\0${hit.scope}\0${hit.datasetId}\0${hit.id}\0${hit.text}`)
    .digest("hex")
    .slice(0, 24);
  return `cognee:${digest}`;
}

export function registerMemoryTools(
  api: OpenClawPluginApi,
  search: (query: string, options: SearchOptions) => Promise<MemoryToolHit[]>,
): void {
  const references = new Map<string, { owner: string; hit: MemoryToolHit; touchedAt: number }>();
  const maxReferences = 500;
  const maxSearchChars = 4_000;
  const defaultGetLines = 50;
  const maxGetLines = 200;
  const maxGetChars = 12_000;

  const rememberReference = (path: string, owner: string, hit: MemoryToolHit) => {
    references.delete(path);
    references.set(path, { owner, hit, touchedAt: Date.now() });
    while (references.size > maxReferences) references.delete(references.keys().next().value!);
  };

  api.registerTool((ctx) => ({
    label: "Memory Search",
    name: "memory_search",
    description: "Search Cognee durable memory and return exact references with provenance. Use before answering questions about prior work, decisions, people, preferences, or commitments.",
    parameters: searchSchema,
    async execute(_id, raw, signal) {
      const params = raw as { query: string; maxResults?: number; minScore?: number; corpus?: string };
      if (params.corpus === "wiki") return jsonResult({
        results: [], provider: "cognee", disabled: true, unavailable: true,
        error: "Cognee does not provide the compiled wiki corpus",
      });
      try {
        if (signal?.aborted) throw new Error("memory search aborted");
        const owner = ownerKey(ctx);
        if (!owner) throw new Error("memory search requires an agent session context");
        const hits = await search(params.query, { ...ctx, maxResults: params.maxResults, minScore: params.minScore, signal });
        const results = hits.map((hit) => {
          const path = referenceFor(hit, owner);
          rememberReference(path, owner, hit);
          const surfacedText = hit.text.length > maxSearchChars
            ? `${hit.text.slice(0, maxSearchChars)}…[truncated; use memory_get]`
            : hit.text;
          return {
            reference: path,
            path,
            id: hit.id,
            text: surfacedText,
            score: hit.score,
            scope: hit.scope,
            corpus: "memory",
            source: hit.source ?? "cognee",
            time: hit.time ?? null,
            provenance: {
              provider: "cognee",
              datasetName: hit.datasetName,
              cogneeId: hit.id,
              sourcePath: hit.source ?? null,
              updatedAt: hit.time ?? null,
            },
          };
        });
        return jsonResult({ results, provider: "cognee" });
      } catch (error) {
        return jsonResult({
          results: [],
          provider: "cognee",
          disabled: true,
          unavailable: true,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    },
  }), { names: ["memory_search"] });

  api.registerTool((ctx) => ({
    label: "Memory Get",
    name: "memory_get",
    description: "Resolve an exact reference returned by memory_search. References are bounded to the current agent session and expire when the plugin restarts or evicts them.",
    parameters: getSchema,
    async execute(_id, raw) {
      const params = raw as { path: string; from?: number; lines?: number };
      const cached = references.get(params.path);
      const owner = ownerKey(ctx);
      if (!owner || !cached || cached.owner !== owner) {
        return jsonResult({
          path: params.path,
          text: "",
          status: "not_found",
          found: false,
          notFound: true,
          error: "Cognee memory reference not found or expired",
        });
      }
      cached.touchedAt = Date.now();
      const allLines = cached.hit.text.split("\n");
      const from = Math.max(1, params.from ?? 1);
      const count = Math.min(maxGetLines, Math.max(1, params.lines ?? defaultGetLines));
      const selected = allLines.slice(from - 1, from - 1 + count).join("\n");
      const text = selected.length > maxGetChars ? `${selected.slice(0, maxGetChars)}…[truncated]` : selected;
      const consumedLines = Math.min(count, Math.max(0, allLines.length - (from - 1)));
      const truncated = from - 1 + consumedLines < allLines.length || selected.length > maxGetChars;
      return jsonResult({
        path: params.path,
        text,
        from,
        lines: consumedLines,
        truncated,
        ...(truncated ? { nextFrom: from + consumedLines } : {}),
        scope: cached.hit.scope,
        source: cached.hit.source ?? "cognee",
        time: cached.hit.time ?? null,
        provenance: {
          provider: "cognee",
          datasetName: cached.hit.datasetName,
          cogneeId: cached.hit.id,
          sourcePath: cached.hit.source ?? null,
          updatedAt: cached.hit.time ?? null,
        },
      });
    },
  }), { names: ["memory_get"] });
}
