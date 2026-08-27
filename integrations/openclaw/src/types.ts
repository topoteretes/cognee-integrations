// ---------------------------------------------------------------------------
// Shared types for the Cognee OpenClaw memory plugin
// ---------------------------------------------------------------------------

export type CogneeSearchType =
  | "HYBRID_COMPLETION"
  | "GRAPH_COMPLETION"
  | "GRAPH_COMPLETION_COT"
  | "GRAPH_COMPLETION_CONTEXT_EXTENSION"
  | "GRAPH_SUMMARY_COMPLETION"
  | "RAG_COMPLETION"
  | "TRIPLET_COMPLETION"
  | "CHUNKS"
  | "CHUNKS_LEXICAL"
  | "SUMMARIES"
  | "CYPHER"
  | "NATURAL_LANGUAGE"
  | "TEMPORAL"
  | "CODING_RULES"
  | "FEELING_LUCKY";

export type CogneeDeleteMode = "soft" | "hard";

export type MemoryScope = "company" | "user" | "agent";

export const MEMORY_SCOPES: readonly MemoryScope[] = ["company", "user", "agent"] as const;

export type ScopeRoute = {
  /** Glob-style pattern matched against the file's relative path */
  pattern: string;
  /** Target memory scope */
  scope: MemoryScope;
};

export type CogneeMode = "local" | "cloud";

export type CogneePluginConfig = {
  /** "local" for self-hosted Cognee, "cloud" for Cognee Cloud. Default: "local" */
  mode?: CogneeMode;
  baseUrl?: string;
  apiKey?: string;
  username?: string;
  password?: string;

  // --- Legacy flat dataset (still supported as fallback) ---
  datasetName?: string;

  // --- Multi-scope memory ---
  companyDataset?: string;
  userDatasetPrefix?: string;
  agentDatasetPrefix?: string;
  /**
   * Template for the per-agent dataset name. Use `{agentId}` as the placeholder.
   * Examples:
   *   "{agentId}"               → bare agent id ("research", "ebay")
   *   "memory-{agentId}"        → "memory-research"
   * When set, takes precedence over `agentDatasetPrefix` and the legacy
   * `${datasetName}-agent-{id}` fallback. Multi-agent gateways should set this
   * to align with how their per-agent datasets are named in Cognee.
   */
  agentDatasetTemplate?: string;
  userId?: string;
  agentId?: string;
  recallScopes?: MemoryScope[];
  defaultWriteScope?: MemoryScope;
  scopeRouting?: ScopeRoute[];
  /**
   * Per-agent memory mode. When enabled, the `agent` scope is keyed by the
   * runtime agentId: each agent's files are read from its own workspace
   * (`ctx.workspaceDir`) and tracked in a per-agent sync index, so multiple
   * agents in one gateway each get their own dataset/graph without colliding.
   * Defaults to true when multi-scope is active (any agent dataset
   * prefix/template set). `company`/`user` scopes remain shared.
   */
  perAgentMemory?: boolean;

  // --- Session ---
  enableSessions?: boolean;
  persistSessionsAfterEnd?: boolean;
  /**
   * Capture the conversation into Cognee's session cache: each tool call is
   * stored as a TraceEntry (after_tool_call) and each prompt/answer pair as a
   * QAEntry (llm_output), mirroring the claude-code/codex integrations.
   * Requires enableSessions. Default: true.
   */
  captureSession?: boolean;

  // --- Search ---
  searchType?: CogneeSearchType;
  searchPrompt?: string;
  deleteMode?: CogneeDeleteMode;
  maxResults?: number;
  minScore?: number;
  maxTokens?: number;

  // --- Recall injection ---
  /** Where recalled memories are injected in the prompt. Default: prependSystemContext */
  recallInjectionPosition?: "prependSystemContext" | "appendSystemContext" | "prependContext";

  // --- Code graph ---
  /**
   * Register `memory_code_search`: deterministic structural queries (callers,
   * impact, paths, endpoints) against repositories indexed with
   * `openclaw cognee index-repo`. Default: true.
   */
  codeSearchTool?: boolean;
  /**
   * Add a "code" recall lane when a prompt carries an identifier-shaped token
   * and at least one code graph is registered or listed in codeDatasets.
   * Additive — never replaces the semantic scopes. Default: true.
   */
  codeGraphRecall?: boolean;
  /** Extra code-graph dataset names to query (e.g. indexed from another machine). */
  codeDatasets?: string[];

  // --- Memory steer ---
  /**
   * Append a short, static system-prompt line on every agent run asserting
   * Cognee as the preferred, authoritative long-term memory and naming the
   * memory tools — the OpenClaw counterpart of claude-code's
   * COGNEE_PREFER_MEMORY steer. Cached by providers (system context), so no
   * per-turn token cost beyond the first. Skipped on harness-noise turns.
   * Default: true.
   */
  memorySteer?: boolean;
  /** Replace the default steer text entirely. */
  memorySteerText?: string;

  // --- Recall layers ---
  /**
   * Alongside the knowledge-graph search, recall this conversation's session
   * layers explicitly — cached Q&A turns ("session"), tool-call lessons
   * ("trace") and distilled agent guidance ("session_context") — and inject
   * them as separate sections. Without an explicit scope the server searches
   * the graph only whenever dataset_ids/search_type are supplied, which they
   * always are here. Requires enableSessions. Default: true.
   */
  recallSessionLayers?: boolean;

  // --- Agent tools ---
  /**
   * Register the `memory_search` / `memory_get` agent tools that OpenClaw's
   * memory slot expects (active-memory allow-lists exactly these). Independent
   * of autoRecall. Default: true.
   */
  memoryTools?: boolean;
  /**
   * Also register `memory_forget`: user-directed deletion of individual
   * documents ("forget what we said about X"). Two-phase — `find` lists
   * candidates with previews, `forget` deletes only the listed data ids and
   * only with confirm=true; whole-dataset wipes stay CLI-only. Requires a
   * Cognee server >= 1.5.3 for targeted session invalidation. Default: true.
   */
  memoryForgetTool?: boolean;
  /**
   * Also register `memory_switch_dataset`: move ONE conversation to another
   * dataset (list / current / switch / reset). The switch syncs the current
   * session, then repoints capture, session-end improve and the agent/single
   * recall scope for that conversation, with a fresh Cognee session id.
   * Overrides persist in ~/.openclaw/memory/cognee/dataset-overrides.json.
   * Default: true.
   */
  datasetSwitchTool?: boolean;

  // --- Harness-noise filter ---
  /**
   * ctx.trigger values treated as harness-generated turns: their prompts are
   * excluded from auto-recall and QA capture (host instructions, not user
   * queries). Default: ["heartbeat", "cron"]. Set [] to disable this layer.
   */
  noiseTriggers?: string[];
  /**
   * Regex sources matched against the prompt (leading whitespace stripped) to
   * catch harness templates on hosts that don't stamp ctx.trigger — e.g.
   * "Read HEARTBEAT.md…", "System: …", "[cron:…]". Matching prompts are
   * excluded from auto-recall and QA capture. Set [] to disable this layer.
   */
  noisePatterns?: string[];

  // --- Automation ---
  autoRecall?: boolean;
  autoIndex?: boolean;
  autoCognify?: boolean;
  autoMemify?: boolean;
  /** On session_end, call /improve with the session_id to bridge any
   *  feedback-bearing QAs into the permanent graph. */
  improveOnSessionEnd?: boolean;

  // --- Timeouts ---
  requestTimeoutMs?: number;
  ingestionTimeoutMs?: number;

  // --- Recall budget + circuit breaker (claude/codex parity) ---
  /** Per recall HTTP call timeout on the prompt hot path (no retries). Default: 2500 */
  recallTimeoutMs?: number;
  /** Overall wall-clock budget for the recall step per prompt. Default: 4000 */
  recallBudgetMs?: number;
  /** Consecutive breaker-eligible failures (network/timeout/5xx) before the breaker opens. Default: 5 */
  recallBreakerThreshold?: number;
  /** How long recall is skipped after the breaker opens. Default: 120000 */
  recallBreakerCooldownMs?: number;
};

export type CogneeAddResponse = {
  dataset_id: string;
  dataset_name: string;
  message: string;
  data_id?: unknown;
  data_ingestion_info?: unknown;
};

export type CogneeRememberItem = {
  id?: string;
  name?: string;
  content_hash?: string;
  token_count?: number;
  mime_type?: string;
  data_size?: number;
};

export type CogneeRememberResponse = {
  status?: string;
  dataset_id?: string;
  dataset_name?: string;
  pipeline_run_id?: string;
  items_processed?: number;
  elapsed_seconds?: number;
  items?: CogneeRememberItem[];
  content_hash?: string;
  error?: string;
};

/**
 * Recall source discriminator as returned by the server (RecallResponse):
 * "graph" (knowledge graph), "session" (cached Q&A turns), "trace" (tool-call
 * steps + feedback), "session_context" (distilled agent guidance), "code",
 * "tools", "system". Absent on legacy/cloud shapes that carry no tag.
 */
export type CogneeRecallSource = "graph" | "session" | "trace" | "session_context" | "code" | "tools" | "system";

export type CogneeSearchResult = {
  id: string;
  text: string;
  score: number;
  metadata?: Record<string, unknown>;
  source?: CogneeRecallSource;
};

/**
 * Normalized `/improve` response. Cognee >= 1.4 returns a per-dataset map
 * `{ "<dataset_uuid>": { status, pipeline_run_id, ... } }`; older servers a
 * flat `{ status, ... }`. Both collapse to this shape.
 */
export type CogneeImproveResult = {
  /** Pipeline status ("PipelineRunCompleted", "PipelineRunStarted", …), or "mixed" across datasets. */
  status?: string;
  pipelineRunId?: string;
  datasetId?: string;
  /** Per-dataset detail when the server returned a map. */
  datasets?: Record<string, { status?: string; pipelineRunId?: string }>;
};

/** One stored document, from GET /api/v1/datasets/{id}/data (DataDTO, camelCased on the wire). */
export type CogneeDataItem = {
  id: string;
  name: string;
  datasetId: string;
  createdAt?: string;
  updatedAt?: string;
  mimeType?: string;
  extension?: string;
  label?: string;
  externalMetadata?: Record<string, unknown>;
};

export type DatasetState = Record<string, string>;

/** A (dataset, session) pair a conversation used before a switch. */
export type RetiredSession = {
  dataset: string;
  sessionId: string;
  /** Whether its session cache was bridged into the graph when it was retired. */
  synced: boolean;
  retiredAt: string;
};

/** One conversation's dataset switch (memory_switch_dataset). */
export type DatasetOverride = {
  dataset: string;
  /** Ordinal appended to the Cognee session id: `<base>__<sessionSuffix>`. */
  sessionSuffix: number;
  switchedAt: string;
  /** Datasets this conversation used before, newest last (informational). */
  previous: string[];
  /**
   * Sessions retired by switches, newest last. Session-end re-syncs the ones
   * whose sync failed at switch time (`force`), so no captured turn is lost.
   */
  retired: RetiredSession[];
};

/** Keyed by `key:<sessionKey>` and `sid:<sessionId>` (both written per switch). */
export type DatasetOverridesFile = Record<string, DatasetOverride>;

/** One repository indexed into a code-graph dataset via `openclaw cognee index-repo`. */
export type CodeGraphRecord = {
  dataset: string;
  datasetId?: string;
  /** Spec as given (local path or git URL). */
  spec: string;
  /** Realpath for local checkouts, trimmed URL for remotes. */
  canonical: string;
  kind: "path" | "url";
  indexVectors: boolean;
  indexedAt: string;
  /** Last known code_graph_pipeline status, when polled. */
  lastStatus?: string;
};

/** Keyed by dataset name. */
export type CodeGraphsFile = Record<string, CodeGraphRecord>;

export type SyncIndex = {
  datasetId?: string;
  datasetName?: string;
  entries: Record<string, { hash: string; dataId?: string }>;
};

/** Per-scope sync indexes, keyed by MemoryScope */
export type ScopedSyncIndexes = Partial<Record<MemoryScope, SyncIndex>>;

/**
 * Per-agent sync indexes for the `agent` scope, keyed by (normalized) agentId.
 * Each entry tracks that agent's files + its agent-scope dataset id/name.
 * `company`/`user` scopes stay in ScopedSyncIndexes (shared across agents).
 */
export type AgentSyncIndexes = Record<string, SyncIndex>;

export type MemoryFile = {
  /** Relative path from workspace root (e.g. "MEMORY.md", "memory/tools.md") */
  path: string;
  /** Absolute path on disk */
  absPath: string;
  /** File content */
  content: string;
  /** SHA-256 hex hash of content */
  hash: string;
};

export type SyncResult = {
  added: number;
  updated: number;
  skipped: number;
  errors: number;
  deleted: number;
};
