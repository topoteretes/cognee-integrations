/**
 * Pure request/response shaping for the Memory resource (remember, recall,
 * forget). No n8n runtime imports so everything here is unit-testable; the
 * node wraps these in preSend/postReceive hooks and converts thrown Errors
 * into NodeOperationErrors.
 */

export const RECALL_AUTO = 'AUTO';

/** Cognee's own default dataset, used when no dataset name is given. */
export const DEFAULT_DATASET_NAME = 'main_dataset';

function cleanList(values: unknown): string[] {
  if (!Array.isArray(values)) return [];
  return values
    .map((value) => (typeof value === 'string' ? value.trim() : String(value ?? '').trim()))
    .filter((value) => value.length > 0);
}

function nonEmpty(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : undefined;
}

/** Accept either an object or a JSON string for structured fields. */
function parseJsonField(value: unknown, label: string): unknown {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'string') return value;
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  let parsed: unknown;
  let valid = true;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    valid = false;
  }
  if (!valid) throw new Error(`${label} must be valid JSON`);
  return parsed;
}

// ---------------------------------------------------------------------------
// Recall
// ---------------------------------------------------------------------------

export interface RecallOptions {
  datasetIds?: string[];
  sessionId?: string;
  scope?: string[];
  onlyContext?: boolean;
  contextFormat?: string;
  contextProfile?: string;
  nodeName?: string[];
  systemPrompt?: string;
  includeReferences?: boolean;
  verbose?: boolean;
}

export interface RecallParams {
  query: string;
  /** A Cognee SearchType, or RECALL_AUTO to let the server route the query. */
  searchType: string;
  datasets?: string[];
  topK?: number;
  options?: RecallOptions;
}

export function buildRecallPayload(params: RecallParams): Record<string, unknown> {
  const query = nonEmpty(params.query);
  if (!query) throw new Error('Query is required');

  const options = params.options ?? {};
  const payload: Record<string, unknown> = {
    query,
    search_type: params.searchType === RECALL_AUTO ? null : params.searchType,
  };

  const datasets = cleanList(params.datasets);
  if (datasets.length) payload.datasets = datasets;

  const datasetIds = cleanList(options.datasetIds);
  if (datasetIds.length) payload.dataset_ids = datasetIds;

  if (typeof params.topK === 'number' && params.topK > 0) payload.top_k = Math.floor(params.topK);

  const sessionId = nonEmpty(options.sessionId);
  if (sessionId) payload.session_id = sessionId;

  const scope = Array.from(new Set(cleanList(options.scope)));
  if (scope.length === 1) payload.scope = scope[0];
  else if (scope.length > 1) payload.scope = scope;

  if (options.onlyContext) {
    payload.only_context = true;
    const contextFormat = nonEmpty(options.contextFormat);
    if (contextFormat) payload.context_format = contextFormat;
  }

  const contextProfile = nonEmpty(options.contextProfile);
  if (contextProfile) payload.context_profile = contextProfile;

  const nodeName = cleanList(options.nodeName);
  if (nodeName.length) payload.node_name = nodeName;

  const systemPrompt = nonEmpty(options.systemPrompt);
  if (systemPrompt) payload.system_prompt = systemPrompt;

  if (options.includeReferences) payload.include_references = true;
  if (options.verbose) payload.verbose = true;

  return payload;
}

/**
 * Flatten the discriminated recall response into one uniform row per hit:
 * always { source, text } plus a few source-specific fields. The raw entry
 * is preserved under `raw` so nothing is lost.
 */
export function simplifyRecallResult(entry: unknown): Record<string, unknown> {
  if (typeof entry === 'string') {
    return { source: 'context', text: entry };
  }
  if (!entry || typeof entry !== 'object') {
    return { source: 'unknown', text: entry == null ? '' : String(entry) };
  }

  const item = entry as Record<string, unknown>;
  const source = typeof item.source === 'string' ? item.source : 'unknown';
  const str = (value: unknown): string =>
    value == null ? '' : typeof value === 'string' ? value : JSON.stringify(value);

  switch (source) {
    case 'session':
      return {
        source,
        text: str(item.answer),
        question: item.question ?? '',
        answer: item.answer ?? '',
        qa_id: item.qa_id ?? null,
        time: item.time ?? null,
        feedback_score: item.feedback_score ?? null,
        raw: item,
      };
    case 'trace':
      return {
        source,
        text: str(item.memory_context) || str(item.method_return_value),
        origin_function: item.origin_function ?? '',
        status: item.status ?? '',
        trace_id: item.trace_id ?? null,
        raw: item,
      };
    case 'session_context':
      return {
        source,
        text: str(item.content),
        context_profile: item.context_profile ?? null,
        raw: item,
      };
    case 'tools':
      return {
        source,
        text: str(item.text),
        tool_name: item.tool_name ?? null,
        question: item.question ?? null,
        success: item.success ?? null,
        error: item.error ?? null,
        raw: item,
      };
    case 'system':
      return { source, text: str(item.text), status: item.status ?? null, raw: item };
    case 'graph':
    case 'code':
    default:
      return {
        source,
        text: str(item.text ?? item.search_result ?? item.answer ?? item),
        kind: item.kind ?? null,
        score: item.score ?? null,
        dataset_name: item.dataset_name ?? null,
        dataset_id: item.dataset_id ?? null,
        raw: item,
      };
  }
}

// ---------------------------------------------------------------------------
// Remember Entry
// ---------------------------------------------------------------------------

export type MemoryEntryType = 'qa' | 'trace' | 'feedback';

export interface RememberEntryParams {
  entryType: MemoryEntryType;
  sessionId: string;
  datasetName?: string;
  datasetId?: string;
  /**
   * Allow a qa entry with only one side filled. Cognee stores half-filled Q&A
   * itself (its own session writes use an empty question), and the chat-memory
   * sub-node needs it so a single inserted message round-trips as one message
   * rather than a pair padded with placeholder text. The Remember Entry
   * operation leaves this off and keeps requiring both sides.
   */
  allowPartialQa?: boolean;
  /** Type-specific fields; irrelevant ones are ignored. */
  fields: {
    question?: string;
    answer?: string;
    context?: string;
    feedbackText?: string;
    feedbackScore?: number;
    originFunction?: string;
    status?: string;
    methodParams?: unknown;
    methodReturnValue?: unknown;
    memoryQuery?: string;
    memoryContext?: string;
    errorMessage?: string;
    qaId?: string;
  };
}

export function buildRememberEntryPayload(params: RememberEntryParams): Record<string, unknown> {
  const sessionId = nonEmpty(params.sessionId);
  if (!sessionId) throw new Error('Session ID is required for qa, trace and feedback entries');

  const f = params.fields ?? {};
  let entry: Record<string, unknown>;

  switch (params.entryType) {
    case 'qa': {
      const question = nonEmpty(f.question);
      const answer = nonEmpty(f.answer);
      if (params.allowPartialQa) {
        if (!question && !answer) throw new Error('A qa entry needs a Question or an Answer');
      } else if (!question || !answer) {
        throw new Error('Question and Answer are required for a qa entry');
      }
      entry = {
        type: 'qa',
        question: question ?? '',
        answer: answer ?? '',
        context: f.context ?? '',
      };
      const feedbackText = nonEmpty(f.feedbackText);
      if (feedbackText) entry.feedback_text = feedbackText;
      if (typeof f.feedbackScore === 'number') entry.feedback_score = Math.round(f.feedbackScore);
      break;
    }
    case 'trace': {
      const originFunction = nonEmpty(f.originFunction);
      if (!originFunction) throw new Error('Origin Function is required for a trace entry');
      entry = {
        type: 'trace',
        origin_function: originFunction,
        status: f.status === 'error' ? 'error' : 'success',
      };
      const methodParams = parseJsonField(f.methodParams, 'Method Params');
      if (methodParams !== undefined) entry.method_params = methodParams;
      const methodReturnValue = parseJsonField(f.methodReturnValue, 'Method Return Value');
      if (methodReturnValue !== undefined) entry.method_return_value = methodReturnValue;
      if (nonEmpty(f.memoryQuery)) entry.memory_query = f.memoryQuery;
      if (nonEmpty(f.memoryContext)) entry.memory_context = f.memoryContext;
      if (nonEmpty(f.errorMessage)) entry.error_message = f.errorMessage;
      break;
    }
    case 'feedback': {
      const qaId = nonEmpty(f.qaId);
      if (!qaId) throw new Error('QA ID is required for a feedback entry');
      entry = { type: 'feedback', qa_id: qaId };
      const feedbackText = nonEmpty(f.feedbackText);
      if (feedbackText) entry.feedback_text = feedbackText;
      if (typeof f.feedbackScore === 'number') entry.feedback_score = Math.round(f.feedbackScore);
      if (!feedbackText && typeof f.feedbackScore !== 'number') {
        throw new Error('Provide Feedback Text and/or Feedback Score for a feedback entry');
      }
      break;
    }
    default:
      throw new Error(`Unsupported entry type: ${String(params.entryType)}`);
  }

  const payload: Record<string, unknown> = {
    entry,
    session_id: sessionId,
    dataset_name: nonEmpty(params.datasetName) ?? DEFAULT_DATASET_NAME,
  };
  const datasetId = nonEmpty(params.datasetId);
  if (datasetId) payload.dataset_id = datasetId;
  return payload;
}

// ---------------------------------------------------------------------------
// Forget
// ---------------------------------------------------------------------------

export type ForgetMode = 'dataset' | 'dataItem' | 'everything';

export interface ForgetParams {
  mode: ForgetMode;
  datasetBy?: 'name' | 'id';
  datasetName?: string;
  datasetId?: string;
  dataId?: string;
  memoryOnly?: boolean;
  confirmEverything?: boolean;
}

export function buildForgetPayload(params: ForgetParams): Record<string, unknown> {
  if (params.mode === 'everything') {
    if (!params.confirmEverything) {
      throw new Error(
        'Forget Everything permanently deletes all datasets and data you own. Enable the confirmation toggle to proceed.',
      );
    }
    return { everything: true };
  }

  const payload: Record<string, unknown> = {};
  if (params.datasetBy === 'id') {
    const datasetId = nonEmpty(params.datasetId);
    if (!datasetId) throw new Error('Dataset ID is required');
    payload.dataset_id = datasetId;
  } else {
    const datasetName = nonEmpty(params.datasetName);
    if (!datasetName) throw new Error('Dataset Name is required');
    payload.dataset = datasetName;
  }

  if (params.mode === 'dataItem') {
    const dataId = nonEmpty(params.dataId);
    if (!dataId) throw new Error('Data ID is required when forgetting a single data item');
    payload.data_id = dataId;
  } else if (params.mode !== 'dataset') {
    throw new Error(`Unsupported forget mode: ${String(params.mode)}`);
  }

  if (params.memoryOnly) payload.memory_only = true;
  return payload;
}

// ---------------------------------------------------------------------------
// Graph model (shared by Remember and Cognify)
// ---------------------------------------------------------------------------

/**
 * Validate a user-supplied graph model schema. Accepts an object or a JSON
 * string and returns the parsed object. Cognee requires a top-level "title"
 * key and rejects anything else with a 400, so check it here for a clearer
 * message. Returns undefined for empty input.
 */
export function parseGraphModel(value: unknown): Record<string, unknown> | undefined {
  if (value === undefined || value === null) return undefined;
  let parsed: unknown = value;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed || trimmed === '{}') return undefined;
    parsed = parseJsonField(trimmed, 'Graph Model');
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Graph Model must be a JSON object');
  }
  const model = parsed as Record<string, unknown>;
  if (Object.keys(model).length === 0) return undefined;
  if (typeof model.title !== 'string' || !model.title.trim()) {
    throw new Error('Graph Model must include a top-level "title" string');
  }
  return model;
}

// ---------------------------------------------------------------------------
// Improve (session -> graph promotion)
// ---------------------------------------------------------------------------

export interface ImproveParams {
  /** Sessions whose cache the server bridges into the dataset's graph. */
  sessionIds: string[];
  datasetName?: string;
  /** Takes precedence over the name; required for a dataset shared with you. */
  datasetId?: string;
  /** Background the cognify-heavy stages (default true). */
  runInBackground?: boolean;
}

/**
 * POST /v1/improve body for promoting session memory into the knowledge
 * graph. The server reads its own session cache, so no session text travels
 * in the request. Improve is idempotent per session (every stage is guarded by
 * a per-session watermark), so re-submitting a session only processes what was
 * added since the last run.
 */
export function buildImprovePayload(params: ImproveParams): Record<string, unknown> {
  const sessionIds = Array.from(new Set(cleanList(params.sessionIds)));
  if (!sessionIds.length) throw new Error('At least one Session ID is required');

  const payload: Record<string, unknown> = {
    session_ids: sessionIds,
    dataset_name: nonEmpty(params.datasetName) ?? DEFAULT_DATASET_NAME,
    run_in_background: params.runInBackground !== false,
  };
  const datasetId = nonEmpty(params.datasetId);
  if (datasetId) payload.dataset_id = datasetId;
  return payload;
}

export interface ImproveOutcome {
  /**
   * `submitted`: the server accepted the run. `busy`: the server answered an
   * empty object, meaning its per-session improve lock is held by another run
   * that will persist everything above the watermark, so nothing is lost and
   * nothing should be retried.
   */
  status: 'submitted' | 'busy';
  /**
   * On a busy answer, whether the run holding the lock was asked to run again
   * afterwards (cognee >= 1.6 reports `rerun_requested`). Only then does a
   * busy answer cover the submitted sessions.
   */
  rerunRequested: boolean;
  /** Per-dataset pipeline statuses when the server reports them. */
  datasets: Record<string, { status?: string; pipelineRunId?: string }>;
}

/**
 * Collapse the `/v1/improve` response to one shape. Cognee 1.4 and 1.5 answer
 * `{ "<dataset_uuid>": { status, pipeline_run_id, ... }, ... }`; 1.6 a flat
 * `{ dataset_id, status, stages: [...] }`; older servers a flat
 * `{ status, pipeline_run_id }`. A lock-skipped run is `{}` on older servers and
 * a stage list of `lock_held` skips on 1.6.
 */
export function summarizeImproveResponse(body: unknown): ImproveOutcome {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return { status: 'submitted', rerunRequested: false, datasets: {} };
  }
  const record = body as Record<string, unknown>;
  const str = (value: unknown): string | undefined =>
    typeof value === 'string' ? value : undefined;
  const rerunRequested = record.rerun_requested === true;

  if (Object.keys(record).length === 0) {
    return { status: 'busy', rerunRequested: false, datasets: {} };
  }

  if (str(record.status) !== undefined || str(record.pipeline_run_id) !== undefined) {
    // cognee >= 1.6 reports per-stage outcomes; a run that found the
    // per-session lock held skips every stage with reason `lock_held`.
    const stages = Array.isArray(record.stages) ? (record.stages as unknown[]) : [];
    const lockHeld =
      stages.length > 0 &&
      stages.every((stage) => {
        const s = stage as Record<string, unknown> | null;
        return s?.status === 'skipped' && s?.reason === 'lock_held';
      });
    const key = str(record.dataset_id) ?? 'default';
    return {
      status: lockHeld ? 'busy' : 'submitted',
      rerunRequested,
      datasets: {
        [key]: { status: str(record.status), pipelineRunId: str(record.pipeline_run_id) },
      },
    };
  }

  const datasets: ImproveOutcome['datasets'] = {};
  for (const [datasetId, value] of Object.entries(record)) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) continue;
    const inner = value as Record<string, unknown>;
    datasets[datasetId] = { status: str(inner.status), pipelineRunId: str(inner.pipeline_run_id) };
  }
  return { status: 'submitted', rerunRequested, datasets };
}
