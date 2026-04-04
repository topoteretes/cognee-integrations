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
  userId?: string;
  agentId?: string;
  recallScopes?: MemoryScope[];
  defaultWriteScope?: MemoryScope;
  scopeRouting?: ScopeRoute[];

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

  // --- Timeouts ---
  requestTimeoutMs?: number;
  ingestionTimeoutMs?: number;
};

export type CogneeAddResponse = {
  dataset_id: string;
  dataset_name: string;
  message: string;
  data_id?: unknown;
  data_ingestion_info?: unknown;
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
