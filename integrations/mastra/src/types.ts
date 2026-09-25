/**
 * Config surface and wire DTOs for cognee's HTTP API. No I/O — client.ts is
 * the only file that talks to the network. Wire shapes are checked against
 * cognee's route handlers rather than assumed from docs, since several
 * diverge from what the docs suggest.
 */

// -- Search types -------------------------------------------------------------

/** cognee's retrieval strategies, verbatim from SearchType(str, Enum) in
 *  cognee/modules/search/types/SearchType.py. A plain string union, not a
 *  TS enum, so resolveConfig can round-trip COGNEE_SEARCH_TYPE with no
 *  runtime object to import. */
export type SearchType =
  | "SUMMARIES"
  | "CHUNKS"
  | "RAG_COMPLETION"
  | "HYBRID_COMPLETION"
  | "TRIPLET_COMPLETION"
  | "GRAPH_COMPLETION"
  | "GRAPH_COMPLETION_DECOMPOSITION"
  | "GRAPH_SUMMARY_COMPLETION"
  | "CYPHER"
  | "NATURAL_LANGUAGE"
  | "GRAPH_COMPLETION_COT"
  | "GRAPH_COMPLETION_CONTEXT_EXTENSION"
  | "FEELING_LUCKY"
  | "TEMPORAL"
  | "CODING_RULES"
  | "CHUNKS_LEXICAL"
  | "AGENTIC_COMPLETION"
  | "CODE"
  | "GRAPH_REPORT"
  | "SKILLS";

/** Shape of an `only_context: true` recall/search result. */
export type ContextFormat = "context" | "prompt";

// -- Config surface (resolveConfig reads process.env against this) ----------

export interface CogneeRecallConfig {
  enabled?: boolean;
  searchType?: SearchType;
  topK?: number;
  /** Skip recall for queries shorter than this (e.g. "ok", "yes"). */
  minQueryLength?: number;
  /** Per-call timeout; disables retry when set. */
  timeoutMs?: number;
  /** Wall-clock cap the input processor must respect. */
  budgetMs?: number;
  includeReferences?: boolean;
}

export interface CogneeWriteConfig {
  mode?: "always" | "assistant-only" | "never";
  runInBackground?: boolean;
  /** Per-turn truncation cap on write payload size. */
  maxChars?: number;
}

export interface CogneeToolsConfig {
  /** Gates cognee_forget out of createCogneeTools() when false. */
  enableForget?: boolean;
  /** Per-call budget for the cognee_* tools; since a per-call timeoutMs
   *  disables retries (client.ts), reads get 0 retries here while writes
   *  keep 1. */
  timeoutMs?: number;
}

/** Used only when apiKey is absent. */
export interface CogneeAuthConfig {
  email?: string;
  password?: string;
}

/** Full config surface; resolution order is explicit argument -> env var ->
 *  default, implemented by config.ts's resolveConfig — nothing else in this
 *  package reads process.env directly. */
export interface CogneeMastraConfig {
  baseUrl?: string;
  /** Wins over `auth` when both are set. */
  apiKey?: string;
  auth?: CogneeAuthConfig;

  dataset?: string;
  datasetPrefix?: string;
  /** Mapping lives in scope.ts. */
  scope?: "tagged" | "dataset-per-resource";
  nodeSet?: string[];

  recall?: CogneeRecallConfig;
  write?: CogneeWriteConfig;
  tools?: CogneeToolsConfig;

  /** Distinct from recall.timeoutMs. */
  requestTimeoutMs?: number;
  /** Disabled whenever a per-call timeoutMs is set (client.ts). */
  retries?: number;
  /** Debug logging must redact apiKey/password. */
  debug?: boolean;
  fetch?: typeof fetch;
}

// -- Wire DTOs — /health ------------------------------------------------------

/** GET /health. 200 body is {status:"ready", health, version}; a 503
 *  narrows to {status:"not ready"} and may omit health/version. All fields
 *  optional so a partial/older server body still parses. */
export interface HealthResponse {
  status?: "ready" | "not ready" | string;
  health?: string;
  version?: string;
  reason?: string;
}

// -- Wire DTOs — auth ---------------------------------------------------------

/** POST /api/v1/auth/login, form-encoded (fastapi-users'
 *  OAuth2PasswordRequestForm — username, not email). */
export interface LoginRequest {
  username: string;
  password: string;
}

/** fastapi-users' default bearer-token login response shape. */
export interface LoginResponse {
  access_token: string;
  token_type: string;
}

// -- Wire DTOs — /datasets -----------------------------------------------------

/** DatasetDTO from get_datasets_router.py; both list and create-or-return resolve to this shape. */
export interface DatasetDTO {
  id: string;
  name: string;
  created_at: string;
  updated_at?: string | null;
  owner_id: string;
}

/** POST /api/v1/datasets body (DatasetCreationPayload). */
export interface EnsureDatasetRequest {
  name: string;
}

// -- Wire DTOs — /recall (primary read path) and its /search legacy fallback --

/** Body shared by POST /api/v1/recall and its /search fallback:
 *  RecallPayloadDTO and SearchPayloadDTO declare the same fields this
 *  adapter sends. Only fields used here are typed; the server accepts more. */
export interface RecallRequest {
  query: string;
  search_type?: SearchType;
  datasets?: string[];
  dataset_ids?: string[];
  top_k?: number;
  only_context?: boolean;
  context_format?: ContextFormat;
  include_references?: boolean;
  session_id?: string;
  node_name?: string[];
}

/** One item from POST /api/v1/recall's array; the server defines this as a
 *  discriminated union on source, but it's typed loosely here (optional
 *  fields plus a [key: string]: unknown escape hatch) since format.ts only
 *  needs renderable text plus provenance. RecallHit below is the
 *  normalized shape the rest of the package programs against. */
export interface RecallResponseItem {
  source: "session" | "trace" | "session_context" | "graph" | "code" | "tools" | "skills" | "system" | string;
  text?: string;
  score?: number | null;
  dataset_id?: string | null;
  dataset_name?: string | null;
  metadata?: Record<string, unknown>;
  raw?: Record<string, unknown>;
  structured?: unknown;
  kind?: string;
  search_type?: string;
  [key: string]: unknown;
}

/** Full response body of POST /api/v1/recall. */
export type RecallResponse = RecallResponseItem[];

/** POST /api/v1/search's response item — a free-form {search_result,
 *  dataset_id, dataset_name} wrapper, not /recall's normalized text/source
 *  envelope, despite sharing a request DTO with it. client.ts normalizes
 *  both shapes into RecallHit rather than reusing one parser. */
export interface SearchResponseItem {
  search_result: unknown;
  dataset_id?: string | null;
  dataset_name?: string | null;
}

/** Full response body of the legacy POST /api/v1/search fallback. */
export type SearchResponse = SearchResponseItem[];

// -- Wire DTOs — /remember (primary write path) and /remember/entry -----------

/** Multipart form for POST /api/v1/remember; datasetName or datasetId is
 *  required. content_type is never sent — cognee 1.5.4 rejects raw_data
 *  unless content_type='code'. */
export interface RememberRequest {
  raw_data: string[];
  datasetName?: string;
  datasetId?: string;
  session_id?: string;
  node_set?: string[];
  run_in_background?: boolean;
}

/**
 * POST /api/v1/add — fallback leg 1 when /remember is absent. Same shape as RememberRequest minus
 * session_id (add has no session concept).
 */
export interface AddRequest {
  raw_data: string[];
  datasetName?: string;
  datasetId?: string;
  node_set?: string[];
  run_in_background?: boolean;
}

/** POST /api/v1/cognify — fallback leg 2, paired with AddRequest. */
export interface CognifyRequest {
  dataset_ids?: string[];
  datasets?: string[];
  run_in_background?: boolean;
}

/** A Q&A turn for the session cache (cognee/memory/entries.py's QAEntry);
 *  saveTurn() maps question/answer/context onto the Mastra turn. */
export interface QAEntryDTO {
  type: "qa";
  question: string;
  answer: string;
  context?: string;
}

/** JSON body for POST /api/v1/remember/entry. Typed narrowly to the qa
 *  variant this adapter sends; the server also accepts trace/feedback/
 *  skill_run entries this package never constructs. */
export interface RememberEntryRequest {
  entry: QAEntryDTO;
  dataset_name?: string;
  dataset_id?: string;
  session_id?: string;
}

/** Response body common to /remember, /add, /cognify, /remember/entry;
 *  only status is load-bearing — an errored run is still 200-shaped,
 *  surfaced as HTTP 409. */
export interface RememberResult {
  status?: "completed" | "running" | "errored" | string;
  [key: string]: unknown;
}

// -- Wire DTOs — /forget -------------------------------------------------------

/** ForgetPayloadDTO from get_forget_router.py; dataset (not dataset_name)
 *  and dataset_id are mutually exclusive. everything:true is hard-blocked in tools.ts. */
export interface ForgetRequest {
  data_id?: string;
  dataset?: string;
  dataset_id?: string;
  memory_only?: boolean;
  everything?: boolean;
}

// -- Wire DTOs — /datasets/status and /improve --------------------------------

/** GET /api/v1/datasets/status response. Pipeline values are add_pipeline,
 *  cognify_pipeline, code_graph_pipeline — not the bare "cognify". A
 *  single-pipeline query returns a flat {dataset_id: status} map. */
export type { PipelineStatusValue as PipelineRunStatusValue } from "./pipeline-status.js";

export type DatasetStatusResponse = Record<string, string | Record<string, string>>;

/** POST /api/v1/improve body. Optional, off by default. */
export interface ImproveRequest {
  dataset_id: string;
  session_ids: string[];
}

// -- Adapter-internal types — normalized recall hit. --------------------------

/** The shape format.ts/processors.ts/tools.ts program against; client.ts's
 *  recall() normalizes both /recall and /search shapes into this. */
export interface RecallHit {
  text: string;
  score?: number | null;
  datasetId?: string | null;
  datasetName?: string | null;
  source?: string;
  metadata?: Record<string, unknown>;
}
