/**
 * Happy-path response bodies for every cognee HTTP route `mock-cognee.ts`
 * serves by default; `routes`/`failFirstN`/`latencyMs` overrides in
 * integration/e2e tests build on top of these. Shapes match `src/types.ts`'s
 * wire DTOs — see that file's header for the source each one traces to.
 *
 * Pure data: no I/O, no `process.env` reads.
 */

import type {
  DatasetDTO,
  DatasetStatusResponse,
  HealthResponse,
  LoginResponse,
  RecallResponse,
  RememberResult,
  SearchResponse,
} from "../../src/types.js";

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

/**
 * Fixed token `mock-cognee.ts` issues from `POST /api/v1/auth/login`, and in
 * `requireAuth: "jwt"` mode the only bearer token it accepts. Exported so
 * auth tests can assert reuse (login once, same token on every later call).
 */
export const MOCK_JWT_TOKEN = "mock-jwt-token";

/** The `X-Api-Key` value `mock-cognee.ts` accepts in `requireAuth: "apiKey"` mode. */
export const MOCK_API_KEY = "test-api-key";

/** fastapi-users bearer login response for `POST /api/v1/auth/login`. */
export const LOGIN_RESPONSE: LoginResponse = {
  access_token: MOCK_JWT_TOKEN,
  token_type: "bearer",
};

// ---------------------------------------------------------------------------
// /health (always public)
// ---------------------------------------------------------------------------

export const HEALTH_RESPONSE: HealthResponse = {
  status: "ready",
  health: "ok",
  version: "0.20.0",
};

// ---------------------------------------------------------------------------
// /datasets
// ---------------------------------------------------------------------------

export const DATASET_ID = "ds-1";
export const DATASET_NAME = "mastra";

/** `POST /api/v1/datasets`: idempotent create-or-return, single object. */
export const ENSURE_DATASET_RESPONSE: DatasetDTO = {
  id: DATASET_ID,
  name: DATASET_NAME,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: null,
  owner_id: "owner-1",
};

/** `GET /api/v1/datasets` list response. */
export const LIST_DATASETS_RESPONSE: DatasetDTO[] = [ENSURE_DATASET_RESPONSE];

// ---------------------------------------------------------------------------
// /recall (primary read path) and legacy /search
// ---------------------------------------------------------------------------

/**
 * Two hits: one `graph`-sourced (the common case) and one `session`-sourced,
 * so `format.ts`'s normalization (RecallResponseItem → RecallHit) has more
 * than one `source` value to prove it handles generically.
 */
export const RECALL_RESPONSE: RecallResponse = [
  {
    source: "graph",
    text: "The user prefers dark mode and TypeScript over JavaScript.",
    score: 0.91,
    dataset_id: DATASET_ID,
    dataset_name: DATASET_NAME,
    metadata: { node_type: "entity" },
  },
  {
    source: "session",
    text: "Earlier in this thread, the user asked about deployment on Railway.",
    score: 0.74,
    dataset_id: DATASET_ID,
    dataset_name: DATASET_NAME,
    metadata: {},
  },
];

/**
 * `POST /api/v1/recall` with an empty corpus/no matches: the "no results" path `format.ts` must
 * turn into `null`, not an empty block.
 */
export const RECALL_RESPONSE_EMPTY: RecallResponse = [];

/**
 * `POST /api/v1/search` legacy fallback: a different response shape from
 * `/recall` (see `SearchResponseItem`'s doc comment in `types.ts`), using a
 * free-form `search_result` wrapper rather than the normalized `text`/`source`
 * envelope.
 */
export const SEARCH_RESPONSE: SearchResponse = [
  {
    search_result: "The user prefers dark mode and TypeScript over JavaScript.",
    dataset_id: DATASET_ID,
    dataset_name: DATASET_NAME,
  },
];

// ---------------------------------------------------------------------------
// /remember, /remember/entry, /add, /cognify: write paths
// ---------------------------------------------------------------------------

export const REMEMBER_RESPONSE: RememberResult = {
  status: "completed",
  dataset_id: DATASET_ID,
};

export const REMEMBER_ENTRY_RESPONSE: RememberResult = {
  status: "completed",
  dataset_id: DATASET_ID,
  entry_id: "entry-1",
};

export const ADD_RESPONSE: RememberResult = {
  status: "completed",
  dataset_id: DATASET_ID,
  data_ids: ["data-1"],
};

export const COGNIFY_RESPONSE: RememberResult = {
  status: "completed",
  dataset_ids: [DATASET_ID],
};

// ---------------------------------------------------------------------------
// /forget
// ---------------------------------------------------------------------------

export const FORGET_RESPONSE: RememberResult = {
  status: "completed",
};

// ---------------------------------------------------------------------------
// /datasets/status is polled only by the live smoke test; query-shaped, so
// the default is a function of the requested dataset id, not a constant.
// ---------------------------------------------------------------------------

export function datasetStatusResponse(datasetId: string = DATASET_ID): DatasetStatusResponse {
  // The enum-name spelling a live cognee 1.5.4 returns, not the lowercase
  // "completed" of older API docs.
  return { [datasetId]: "DATASET_PROCESSING_COMPLETED" };
}

// ---------------------------------------------------------------------------
// /improve
// ---------------------------------------------------------------------------

export const IMPROVE_RESPONSE: RememberResult = {
  status: "completed",
  dataset_id: DATASET_ID,
};
