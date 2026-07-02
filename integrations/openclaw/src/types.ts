// ---------------------------------------------------------------------------
// Shared types for the Cognee OpenClaw memory plugin
// ---------------------------------------------------------------------------

export type CogneeSearchType =
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
  /** Timeout for read operations (search / recall). Defaults to requestTimeoutMs. */
  recallTimeoutMs?: number;

  // --- Circuit breaker ---
  /** Trip the breaker after repeated backend failures so a down server isn't
   *  hammered on every call. Default: true. */
  breakerEnabled?: boolean;
  /** Consecutive UNREACHABLE / 5xx failures before the breaker opens. Default: 5. */
  breakerThreshold?: number;
  /** How long the breaker stays open before allowing a retry, in ms. Default: 120000. */
  breakerCooldownMs?: number;
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

export type CogneeSearchResult = {
  id: string;
  text: string;
  score: number;
  metadata?: Record<string, unknown>;
};

export type DatasetState = Record<string, string>;

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
