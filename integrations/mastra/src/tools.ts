/**
 * `createCogneeSearchTool` / `createCogneeAskTool` / `createCogneeRememberTool` /
 * `createCogneeForgetTool` and the `createCogneeTools(config)` collection
 * factory — the explicit, agent-driven half of this package's dual surface.
 * Follows the Mastra/tavily tool-factory idiom: a function returning a
 * `Tool` built with `createTool()`, using its own Zod `inputSchema`/
 * `outputSchema`/`execute(inputData, context)` shape.
 */

import { z } from "zod";
import { createTool, type Tool, type ToolSet } from "@mastra/core/tools";

import { CogneeClient } from "./client.js";
import { CogneeApiError } from "./errors.js";
import { resolveConfig, type ResolvedCogneeConfig } from "./config.js";
import { resolveScope, sanitizeId } from "./scope.js";
import type { CogneeMastraConfig, RecallHit } from "./types.js";

/**
 * `Tool<any, ...>`, not bare `Tool`: `unknown` defaults make `execute`'s `inputData` contravariant,
 * so a concrete `Tool<{query: string}, ...>` isn't assignable to `Tool<unknown, ...>`; `any`
 * matches Mastra's own `ToolSet`.
 */
type AnyCogneeTool = Tool<any, any, any, any, any, any, any>;

// ---- Shared: lazy client + resolved-config plumbing ----

/**
 * Every factory resolves `process.env` exactly once via `resolveConfig()`; tools operate on the
 * fully-defaulted `ResolvedCogneeConfig`, never a raw `CogneeMastraConfig` field directly.
 */
interface FactoryContext {
  /**
   * Lazily constructs (and thereafter reuses) the shared `CogneeClient` — see
   * `resolveFactoryContext` below.
   */
  getClient: () => CogneeClient;
  resolved: ResolvedCogneeConfig;
}

export interface CogneeToolFactoryOptions {
  /**
   * Reuse an already-constructed client (e.g. the one `withCognee()`'s processors already hold)
   * instead of building a new one.
   */
  client?: CogneeClient;
  /**
   * Config resolved via `resolveConfig()` when `client` (or the internal `shared` context
   * `createCogneeTools()` passes) is absent.
   */
  config?: CogneeMastraConfig;
  /**
   * @internal Set only by `createCogneeTools()` so every tool it builds shares one client/config.
   * Not meant for external callers — use `client` or `config`.
   */
  shared?: FactoryContext;
}

/**
 * Resolves config once, returning a `getClient()` accessor that lazily builds (and reuses) the
 * `CogneeClient`; only calling a tool's `execute` can throw for a missing credential, never this
 * call itself.
 */
function resolveFactoryContext(options: CogneeToolFactoryOptions): FactoryContext {
  if (options.shared) return options.shared;

  const resolved = resolveConfig(options.config ?? {});
  let cached: CogneeClient | undefined = options.client;
  const getClient = (): CogneeClient => {
    if (!cached) cached = new CogneeClient(resolved);
    return cached;
  };
  return { getClient, resolved };
}

// ---- Shared: error/result helpers ----

/** Every tool's `execute` funnels a caught error through this — never a throw. */
function toErrorMessage(error: unknown): string {
  if (error instanceof CogneeApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

/**
 * `RecallHit` -> the model-facing shape declared by every tool's `RecallHitSchema` below — `null`,
 * not `undefined`, for absent optional fields, matching `.nullable()`'s wire shape.
 */
function toHitDTO(hit: RecallHit): z.infer<typeof RecallHitSchema> {
  return {
    text: hit.text,
    score: hit.score ?? null,
    source: hit.source,
    datasetId: hit.datasetId ?? null,
    datasetName: hit.datasetName ?? null,
  };
}

/**
 * `cognee_remember`'s `metadata` -> `node_set` tags. Both key and value pass through `sanitizeId`
 * (scope.ts) — metadata keys are caller-supplied, unlike `scope.ts`'s own hardcoded
 * `resource:`/`thread:` tag kinds, so they need the same collision-safety treatment `sanitizeId`
 * already gives ids.
 */
function metadataToTags(metadata: Record<string, string | number | boolean> | undefined): string[] {
  if (!metadata) return [];
  return Object.entries(metadata).map(([key, value]) => `${sanitizeId(key)}:${sanitizeId(String(value))}`);
}

// ---- Shared: recall-hit schema (used by both cognee_search and cognee_ask) ----

const RecallHitSchema = z.object({
  text: z.string(),
  score: z.number().nullable().optional(),
  source: z.string().optional(),
  datasetId: z.string().nullable().optional(),
  datasetName: z.string().nullable().optional(),
});

// ---- cognee_search: fast semantic passage recall ----

const SearchInputSchema = z.object({
  query: z.string().min(1).describe("Natural-language question or topic to search cognee's memory for."),
  topK: z.number().int().positive().max(50).optional().describe("Max passages to return. Default 10."),
  nodeName: z
    .array(z.string())
    .optional()
    .describe(
      "Advanced: restrict results to these exact node tags. Usually left unset — the tool already scopes results to the current resource automatically.",
    ),
});

const SearchOutputSchema = z.object({
  results: z.array(RecallHitSchema).optional().describe("Matching passages, most relevant first."),
  error: z.string().optional().describe("Present, and `results` absent, when the search failed."),
});

/**
 * Fast semantic passage recall (`search_type: CHUNKS`, `only_context: true`,
 * no LLM call inside cognee) — the cheap default the model should reach for before `cognee_ask`.
 */
export function createCogneeSearchTool(options: CogneeToolFactoryOptions = {}): AnyCogneeTool {
  const { getClient, resolved } = resolveFactoryContext(options);

  return createTool({
    id: "cognee_search",
    description:
      "Search cognee's long-term memory for relevant passages. Fast and cheap — no LLM call happens inside " +
      "cognee for this tool, just a vector/keyword lookup. Use this as the default for 'what do we know about " +
      "X', 'find anything about Y', or 'have we discussed Z before' questions. Returns raw text passages with a " +
      "relevance score, not a synthesized answer — if you need cognee to reason over the graph and produce a " +
      "written answer instead of passages, use cognee_ask (that one is slow and expensive; prefer this tool first).",
    inputSchema: SearchInputSchema,
    outputSchema: SearchOutputSchema,
    execute: async (input, context) => {
      return context.observe.span("cognee.cognee_search", async () => {
        try {
          const scope = resolveScope(resolved, { threadId: context.agent?.threadId, resourceId: context.agent?.resourceId });
          const nodeName = [...scope.nodeNameFilter, ...(input.nodeName ?? [])];
          const hits = await getClient().recall(
            {
              query: input.query,
              search_type: "CHUNKS",
              datasets: [scope.dataset],
              top_k: input.topK ?? resolved.recall.topK,
              only_context: true,
              session_id: scope.sessionId || undefined,
              node_name: nodeName.length > 0 ? nodeName : undefined,
            },
            // Explicit bounded budget: a bare call would inherit the
            // client-wide 30s x (retries+1) worst case. `timeoutMs` alone implies 0 retries
            // (client.ts) — right for a read.
            { timeoutMs: resolved.tools.timeoutMs },
          );
          return { results: hits.map(toHitDTO) };
        } catch (error) {
          return { error: toErrorMessage(error) };
        }
      });
    },
  });
}

// ---- cognee_ask: graph Q&A, expensive/slow ----

const AskInputSchema = z.object({
  question: z.string().min(1).describe("The question to have cognee reason over its knowledge graph and answer."),
  topK: z.number().int().positive().max(50).optional().describe("Max supporting graph nodes to consider. Default 10."),
});

const AskOutputSchema = z.object({
  answer: z.string().optional().describe("Cognee's synthesized answer. Absent (with `error` set) on failure."),
  references: z
    .array(RecallHitSchema)
    .optional()
    .describe("Graph nodes/passages that back the answer (include_references: true)."),
  error: z.string().optional().describe("Present, and `answer` absent, when the request failed."),
});

/**
 * Graph question-answering (`GRAPH_COMPLETION`, `include_references: true`,
 * `only_context: false`) — the only surface here billed against cognee's
 * own API key for an extra LLM call; the tool description warns the model before it calls this.
 */
export function createCogneeAskTool(options: CogneeToolFactoryOptions = {}): AnyCogneeTool {
  const { getClient, resolved } = resolveFactoryContext(options);

  return createTool({
    id: "cognee_ask",
    description:
      "Ask cognee's knowledge graph a question and get back a reasoned, synthesized answer. EXPENSIVE AND SLOW " +
      "compared to cognee_search: this triggers an additional LLM call inside the cognee server (billed to the " +
      "cognee server's own API key, not this conversation's) and typically takes several seconds. Only use this " +
      "when you actually need graph reasoning or a written synthesis — for a quick lookup of relevant passages, " +
      "use cognee_search instead; it is faster and cheaper and usually enough.",
    inputSchema: AskInputSchema,
    outputSchema: AskOutputSchema,
    execute: async (input, context) => {
      return context.observe.span("cognee.cognee_ask", async () => {
        try {
          const scope = resolveScope(resolved, { threadId: context.agent?.threadId, resourceId: context.agent?.resourceId });
          const hits = await getClient().recall(
            {
              query: input.question,
              search_type: "GRAPH_COMPLETION",
              datasets: [scope.dataset],
              top_k: input.topK ?? resolved.recall.topK,
              only_context: false,
              include_references: resolved.recall.includeReferences,
              session_id: scope.sessionId || undefined,
              node_name: scope.nodeNameFilter.length > 0 ? scope.nodeNameFilter : undefined,
            },
            // Same bounded budget as cognee_search. GRAPH_COMPLETION is
            // already slow/expensive; still one bounded attempt, not 30s x (retries+1).
            { timeoutMs: resolved.tools.timeoutMs },
          );
          const answer = hits
            .map((hit) => hit.text)
            .filter((text) => text.trim().length > 0)
            .join("\n\n");
          return {
            answer,
            references: hits.length > 0 ? hits.map(toHitDTO) : undefined,
          };
        } catch (error) {
          return { error: toErrorMessage(error) };
        }
      });
    },
  });
}

// ---- cognee_remember: explicit write ----

const RememberInputSchema = z.object({
  statement: z.string().min(1).describe("The fact or statement to remember, written out in plain language."),
  metadata: z
    .record(z.string(), z.union([z.string(), z.number(), z.boolean()]))
    .optional()
    .describe("Optional key/value tags attached to this memory, e.g. { category: 'preference' }."),
});

const RememberOutputSchema = z.object({
  success: z.boolean().optional(),
  status: z.string().optional().describe("cognee's own pipeline-run status for this write, when available."),
  error: z.string().optional().describe("Present, and `success: false`, when the write failed."),
});

/**
 * Explicit write of a statement/fact. `metadata` folds into `node_set` via
 * `metadataToTags` above — cognee's write endpoints have no separate metadata field.
 */
export function createCogneeRememberTool(options: CogneeToolFactoryOptions = {}): AnyCogneeTool {
  const { getClient, resolved } = resolveFactoryContext(options);

  return createTool({
    id: "cognee_remember",
    description:
      "Explicitly save a fact or statement to cognee's long-term memory, so it can be recalled in future " +
      "conversations (via cognee_search/cognee_ask or the automatic recall this package also provides). Use this " +
      "for durable facts worth remembering beyond this conversation — user preferences, decisions, stable " +
      "context — not for routine conversational turns, which the output processor (if configured) already " +
      "captures on its own.",
    inputSchema: RememberInputSchema,
    outputSchema: RememberOutputSchema,
    execute: async (input, context) => {
      return context.observe.span("cognee.cognee_remember", async () => {
        try {
          const scope = resolveScope(resolved, { threadId: context.agent?.threadId, resourceId: context.agent?.resourceId });
          const result = await getClient().remember(
            {
              raw_data: [input.statement],
              datasetName: scope.dataset,
              session_id: scope.sessionId || undefined,
              node_set: [...scope.nodeSet, ...metadataToTags(input.metadata)],
              run_in_background: resolved.write.runInBackground,
            },
            // Same bounded budget as the read tools, but a write can afford
            // one retry within it — a transient 500 shouldn't cost the model a whole failed call.
            { timeoutMs: resolved.tools.timeoutMs, retries: 1 },
          );
          return { success: true, status: typeof result?.status === "string" ? result.status : undefined };
        } catch (error) {
          return { success: false, error: toErrorMessage(error) };
        }
      });
    },
  });
}

// ---- cognee_forget: destructive, opt-in only ----

const ForgetInputSchema = z.object({
  dataId: z.string().min(1).describe("The cognee data id of the specific memory item to forget."),
});

const ForgetOutputSchema = z.object({
  success: z.boolean().optional(),
  error: z.string().optional().describe("Present, and `success: false`, when the forget request failed."),
});

/**
 * Destructive deletion of one memory item; always forces `memory_only: true`
 * (never `everything: true`), enforced here and independently in
 * `CogneeClient.forget`. Not gated by `enableForget` itself — see `createCogneeTools()` below for
 * that gate.
 */
export function createCogneeForgetTool(options: CogneeToolFactoryOptions = {}): AnyCogneeTool {
  const { getClient, resolved } = resolveFactoryContext(options);

  return createTool({
    id: "cognee_forget",
    description:
      "DESTRUCTIVE: permanently deletes one specific memory item from cognee by its data id (memory_only mode — " +
      "the underlying source data cognee ingested is not deleted, only this memory-graph entry). Only call this " +
      "when the user has explicitly asked to forget, delete, or remove something specific that was remembered. " +
      "Never call this speculatively or to 'clean up' — there is no undo.",
    inputSchema: ForgetInputSchema,
    outputSchema: ForgetOutputSchema,
    execute: async (input, context) => {
      return context.observe.span("cognee.cognee_forget", async () => {
        try {
          // Same bounded write budget as cognee_remember: one retry within
          // `resolved.tools.timeoutMs`.
          await getClient().forget(
            { data_id: input.dataId, memory_only: true },
            { timeoutMs: resolved.tools.timeoutMs, retries: 1 },
          );
          return { success: true };
        } catch (error) {
          return { success: false, error: toErrorMessage(error) };
        }
      });
    },
  });
}

// ---- createCogneeTools: collection factory ----

export interface CogneeToolsFactoryConfig extends CogneeMastraConfig {
  /**
   * Reuse an already-constructed client (e.g. shared with
   * `createCogneeProcessors()`/`withCognee()`) instead of building a new one.
   */
  client?: CogneeClient;
}

/**
 * `createCogneeTools(config)` -> `Record<string, Tool>` (the tavily/
 * perplexity collection-factory pattern). Builds one `CogneeClient` lazily
 * and shares it, and the resolved config, across every tool this call
 * returns. `cognee_forget` is included only when
 * `config.tools.enableForget === true`; the standalone `createCogneeForgetTool` export is never
 * gated.
 */
export function createCogneeTools(config: CogneeToolsFactoryConfig = {}): ToolSet {
  const { client, ...cogneeConfig } = config;
  const shared = resolveFactoryContext({ client, config: cogneeConfig });

  const tools: ToolSet = {
    cognee_search: createCogneeSearchTool({ shared }),
    cognee_ask: createCogneeAskTool({ shared }),
    cognee_remember: createCogneeRememberTool({ shared }),
  };

  if (shared.resolved.tools.enableForget) {
    tools.cognee_forget = createCogneeForgetTool({ shared });
  }

  return tools;
}
