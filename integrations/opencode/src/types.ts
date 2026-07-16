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

export type CogneeMode = "local" | "cloud";

export type CogneePluginConfig = {
  mode?: CogneeMode;
  baseUrl?: string;
  apiKey?: string;
  username?: string;
  password?: string;
  datasetName?: string;
  enableSessions?: boolean;
  searchType?: CogneeSearchType;
  searchPrompt?: string;
  deleteMode?: CogneeDeleteMode;
  maxResults?: number;
  minScore?: number;
  maxTokens?: number;
  autoRecall?: boolean;
  improveOnSessionEnd?: boolean;
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
