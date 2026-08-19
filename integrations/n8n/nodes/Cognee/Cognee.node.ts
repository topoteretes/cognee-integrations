import type {
  IDataObject,
  IExecuteSingleFunctions,
  IHttpRequestOptions,
  IN8nHttpFullResponse,
  INodeExecutionData,
  INodePropertyOptions,
  INodeType,
  INodeTypeDescription,
} from 'n8n-workflow';
import { NodeConnectionTypes } from 'n8n-workflow';

/**
 * Completion search strategies shared by the Search and Recall operations, so
 * the two ops never drift apart. Recall prepends an Auto entry to this list.
 */
const SEARCH_COMPLETION_TYPES: INodePropertyOptions[] = [
  { name: 'GraphCompletion', value: 'GRAPH_COMPLETION' },
  { name: 'ChainOfThought', value: 'GRAPH_COMPLETION_COT' },
  { name: 'RagCompletion', value: 'RAG_COMPLETION' },
];

/**
 * Pull the agent's answer text out of the /v1/search response envelope.
 * AGENTIC_COMPLETION returns the answer wrapped in a list and/or a
 * { search_result: ... } object, so unwrap recursively (mirrors the SDK's
 * unwrap_answer in run_self_improve_skill.py).
 */
function unwrapSearchAnswer(value: unknown): string {
  if (Array.isArray(value)) {
    return value.length ? unwrapSearchAnswer(value[0]) : '';
  }
  if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    for (const key of ['search_result', 'result', 'answer', 'text']) {
      if (key in obj) {
        return unwrapSearchAnswer(obj[key]);
      }
    }
    return JSON.stringify(obj);
  }
  return value == null ? '' : String(value);
}

/**
 * Normalise parsed JSON review fields to the canonical shape the workflow
 * expects: { score, missing_instruction, result_summary, dimensions }.
 * LLMs sometimes return equivalent fields under alternate names
 * (average_score, most_impactful_missing_instruction, summary, grades).
 */
function normalizeReviewFields(obj: Record<string, unknown>): Record<string, unknown> {
  const score = obj.score ?? obj.average_score;
  const missing_instruction =
    obj.missing_instruction ?? obj.most_impactful_missing_instruction ?? '';
  const result_summary = obj.result_summary ?? obj.summary ?? '';
  let dimensions = obj.dimensions;
  if (!Array.isArray(dimensions) && obj.grades && typeof obj.grades === 'object') {
    dimensions = Object.entries(obj.grades as Record<string, number>).map(([name, s]) => ({
      name,
      score: s,
    }));
  }
  return { ...obj, score, missing_instruction, result_summary, dimensions: dimensions ?? [] };
}

/**
 * Tolerantly parse the strict-JSON review the prompt asks for. Falls back to
 * extracting the first {...} block if the model wrapped it in prose/fences,
 * and finally falls back to regex extraction of the self-review prose block
 * the model sometimes produces instead of JSON.
 */
function parseReviewJson(text: string): Record<string, unknown> {
  // 1. Pure JSON response
  try {
    return normalizeReviewFields(JSON.parse(text) as Record<string, unknown>);
  } catch { /* fall through */ }

  // 2. JSON block embedded in prose or fenced code block
  const match = text.match(/\{[\s\S]*\}/);
  if (match) {
    try {
      return normalizeReviewFields(JSON.parse(match[0]) as Record<string, unknown>);
    } catch { /* fall through */ }
  }

  // 3. Prose self-review block: "Overall score: 0.94" + per-dimension bullet list
  const scoreMatch = text.match(/[Oo]verall\s+score[:\s]+([0-9]*\.?[0-9]+)/);
  if (scoreMatch) {
    const score = parseFloat(scoreMatch[1]);
    const dimensions: Array<{ name: string; score: number }> = [];
    const dimPattern = /-\s*([\w_]+):\s*([0-9]*\.?[0-9]+)/g;
    let m: RegExpExecArray | null;
    while ((m = dimPattern.exec(text)) !== null) {
      dimensions.push({ name: m[1], score: parseFloat(m[2]) });
    }
    const missingMatch = text.match(/[Mm]issing\s+instruction[:\s]+([^\n]+)/);
    const summaryMatch = text.match(/[Rr]esult\s+summary[:\s]+([^\n]+(?:\n[^\n]+)*?)(?=\n\n|\n[A-Z]|$)/);
    return {
      score,
      dimensions,
      missing_instruction: missingMatch ? missingMatch[1].trim() : '',
      result_summary: summaryMatch ? summaryMatch[1].trim() : '',
      review: text,
    };
  }

  return {};
}

/**
 * postReceive transform for the Review Skill operation: turns the raw search
 * response into a flat item exposing { score, missing_instruction,
 * result_summary, dimensions, review } so the workflow's IF gate can branch on
 * `score` with no extra node. The score is the LLM-emitted mean of the
 * per-dimension scores. On unparseable output, score defaults to 0 (treated as
 * a failing review) and the raw answer is preserved for debugging.
 */
async function parseReviewScore(
  this: IExecuteSingleFunctions,
  _items: INodeExecutionData[],
  response: IN8nHttpFullResponse,
): Promise<INodeExecutionData[]> {
  const answer = unwrapSearchAnswer(response.body);
  const parsed = parseReviewJson(answer);
  const rawScore = Number(parsed.score);
  const parseOk = Number.isFinite(rawScore);
  const score = parseOk ? Math.max(0, Math.min(1, rawScore)) : 0;
  return [
    {
      json: {
        score,
        score_parse_ok: parseOk,
        missing_instruction: (parsed.missing_instruction as string) ?? '',
        result_summary:
          (parsed.result_summary as string) ??
          (parseOk ? '' : 'Could not parse a score from the review; raw answer preserved.'),
        dimensions: parsed.dimensions ?? [],
        review: (parsed.review as string) ?? answer,
        raw_answer: answer,
      },
    },
  ];
}

/**
 * Multi-value string parameters reach preSend as whatever the user's
 * expression resolved to, so a single expression yielding a plain string must
 * become a one-element array rather than being iterated character by character.
 */
function toStringArray(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(String).filter(Boolean);
  return value ? [String(value)] : [];
}

/**
 * preSend for the Recall operation. Builds the whole JSON body in one place:
 * search_type is an explicit null for Auto (the backend's opt-in to
 * auto-routing), and the optional fields (datasets, session_id, node_name) are
 * omitted entirely when empty instead of being sent as null.
 */
async function buildRecallBody(
  this: IExecuteSingleFunctions,
  requestOptions: IHttpRequestOptions,
): Promise<IHttpRequestOptions> {
  const searchType = this.getNodeParameter('recallSearchType', 'AUTO') as string;
  const datasets = toStringArray(this.getNodeParameter('recallDatasets', []));
  const sessionId = this.getNodeParameter('recallSessionId', '') as string;
  const nodeName = toStringArray(this.getNodeParameter('recallNodeName', []));
  const topK = this.getNodeParameter('recallTopK', 15) as number;

  const body: IDataObject = {
    search_type: searchType === 'AUTO' ? null : searchType,
    query: this.getNodeParameter('recallQuery', '') as string,
    scope: this.getNodeParameter('recallScope', 'auto') as string,
  };
  if (datasets.length) body.datasets = datasets;
  if (sessionId) body.session_id = sessionId;
  if (nodeName.length) body.node_name = nodeName;
  if (topK > 0) body.top_k = topK;
  requestOptions.body = body;
  return requestOptions;
}

/** One plain (non-file) field of a multipart/form-data body. */
function multipartField(boundary: string, name: string, value: string): string {
  return `--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`;
}

/**
 * preSend for the Remember operation. /v1/remember is multipart ingest, so the
 * Text field is sent as an uploaded note.txt file part alongside datasetName,
 * datasetId, session_id, run_in_background and one node_set form field per
 * tag. The multipart body is assembled by hand into a Buffer with an explicit
 * boundary header: n8n Cloud forbids the form-data package (no runtime deps
 * for community nodes) and a native WHATWG FormData body is only serialized
 * correctly on hosts bundling a new-enough axios, so neither is portable.
 * This is the one-shot add+cognify path with session attribution and node-set
 * tagging.
 */
async function buildRememberForm(
  this: IExecuteSingleFunctions,
  requestOptions: IHttpRequestOptions,
): Promise<IHttpRequestOptions> {
  const text = this.getNodeParameter('rememberText', '') as string;
  const datasetName = this.getNodeParameter('rememberDatasetName', '') as string;
  const datasetId = this.getNodeParameter('rememberDatasetId', '') as string;
  const sessionId = this.getNodeParameter('rememberSessionId', '') as string;
  const nodeSet = toStringArray(this.getNodeParameter('rememberNodeSet', []));
  const runInBackground = this.getNodeParameter('rememberRunInBackground', true) as boolean;

  const boundary = `----n8nCogneeRemember${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`;
  const parts: string[] = [
    `--${boundary}\r\nContent-Disposition: form-data; name="data"; filename="note.txt"\r\nContent-Type: text/plain\r\n\r\n${text}\r\n`,
    multipartField(boundary, 'datasetName', datasetName),
  ];
  if (datasetId) parts.push(multipartField(boundary, 'datasetId', datasetId));
  if (sessionId) parts.push(multipartField(boundary, 'session_id', sessionId));
  for (const tag of nodeSet) {
    parts.push(multipartField(boundary, 'node_set', tag));
  }
  parts.push(multipartField(boundary, 'run_in_background', runInBackground ? 'true' : 'false'));
  parts.push(`--${boundary}--\r\n`);

  requestOptions.body = Buffer.from(parts.join(''), 'utf-8');
  requestOptions.headers = {
    ...requestOptions.headers,
    'Content-Type': `multipart/form-data; boundary=${boundary}`,
  };
  return requestOptions;
}

export class Cognee implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Cognee',
    name: 'cognee',
    icon: 'file:cognee.svg',
    group: ['transform'],
    usableAsTool: true,
    version: 1,
    subtitle: '={{$parameter["resource"] + ": " + $parameter["operation"]}}',
    description: 'Add text data to a Cognee dataset, build a knowledge graph, search Cognee memory, recall from memory with session and node-set filtering, remember text in one add+cognify call, manage datasets, and run the self-improving skill loop (ingest, review, propose, apply)',
    defaults: {
      name: 'Cognee',
    },
    inputs: [NodeConnectionTypes.Main],
    outputs: [NodeConnectionTypes.Main],
    credentials: [
      {
        name: 'cogneeApi',
        required: true,
      },
    ],
    requestDefaults: {
      baseURL: '={{$credentials.baseUrl}}/api',
      headers: {
        Accept: 'application/json',
        'X-Api-Key': '={{$credentials.apiKey}}',
      },
    },
    properties: [
      {
        displayName: 'Resource',
        name: 'resource',
        type: 'options',
        noDataExpression: true,
        options: [
          { name: 'Add Data', value: 'addData' },
          { name: 'Cognify', value: 'cognify' },
          { name: 'Delete', value: 'delete' },
          { name: 'Recall', value: 'recall' },
          { name: 'Remember', value: 'remember' },
          { name: 'Search', value: 'search' },
          { name: 'Skill', value: 'skill' },
        ],
        default: 'addData',
      },
      {
        displayName: 'Operation',
        name: 'operation',
        type: 'options',
        noDataExpression: true,
        displayOptions: {
          show: {
            resource: ['addData'],
          },
        },
        options: [
          {
            name: 'Add',
            value: 'add',
            action: 'Add data to cognee datasets',
            description: 'Add text_data to a Cognee dataset to "cognify" later in the Cognee memory engine',
            routing: {
              request: {
                method: 'POST',
                url: '/add_text',
                headers: {
                  'Content-Type': 'application/json',
                },
                timeout: 300000, // 5 minutes
              },
            },
          },
        ],
        default: 'add',
      },
      {
        displayName: 'Operation',
        name: 'operation',
        type: 'options',
        noDataExpression: true,
        displayOptions: {
          show: {
            resource: ['cognify'],
          },
        },
        options: [
          {
            name: 'Cognify',
            value: 'cognify',
            action: 'Cognify an existing dataset into memory',
            description: 'After adding text data to a Cognee dataset, trigger cognify to build a knowledge graph based memory from it',
            routing: {
              request: {
                method: 'POST',
                url: '/cognify',
                headers: {
                  'Content-Type': 'application/json',
                },
                timeout: 600000, // 10 minutes
              },
            },
          },
        ],
        default: 'cognify',
      },
      {
        displayName: 'Operation',
        name: 'operation',
        type: 'options',
        noDataExpression: true,
        displayOptions: {
          show: {
            resource: ['search'],
          },
        },
        options: [
          {
            name: 'Search',
            value: 'search',
            action: 'Search in cognee memory',
            description: 'Run a search query in Cognee memory engine',
            routing: {
              request: {
                method: 'POST',
                url: '/search',
                headers: {
                  'Content-Type': 'application/json',
                },
                timeout: 300000, // 5 minutes
              },
            },
          },
        ],
        default: 'search',
      },
      {
        displayName: 'Operation',
        name: 'operation',
        type: 'options',
        noDataExpression: true,
        displayOptions: {
          show: {
            resource: ['recall'],
          },
        },
        options: [
          {
            name: 'Recall',
            value: 'recall',
            action: 'Recall from cognee memory',
            description: 'Memory-oriented search over cognee: like Search, with session, node-set and auto-routing (search_type null)',
            routing: {
              send: {
                preSend: [buildRecallBody],
              },
              request: {
                method: 'POST',
                url: '/v1/recall',
                headers: {
                  'Content-Type': 'application/json',
                },
                timeout: 300000, // 5 minutes
              },
            },
          },
        ],
        default: 'recall',
      },
      {
        displayName: 'Operation',
        name: 'operation',
        type: 'options',
        noDataExpression: true,
        displayOptions: {
          show: {
            resource: ['remember'],
          },
        },
        options: [
          {
            name: 'Remember',
            value: 'remember',
            action: 'Remember data into cognee memory',
            description: 'One-shot add + cognify of text with session attribution and node-set tagging',
            routing: {
              send: {
                preSend: [buildRememberForm],
              },
              request: {
                method: 'POST',
                url: '/v1/remember',
                timeout: 600000, // 10 minutes
              },
            },
          },
        ],
        default: 'remember',
      },
      {
        displayName: 'Operation',
        name: 'operation',
        type: 'options',
        noDataExpression: true,
        displayOptions: {
          show: {
            resource: ['delete'],
          },
        },
        options: [
          {
            name: 'Delete Dataset',
            value: 'deleteDataset',
            action: 'Delete a dataset by its ID',
            description: 'Permanently delete a dataset and all its associated data',
            routing: {
              request: {
                method: 'DELETE',
                url: '=/datasets/{{$parameter["datasetId"]}}',
                timeout: 300000, // 5 minutes
              },
              output: {
                postReceive: [
                  {
                    type: 'set',
                    properties: {
                      value: '={{ { "deleted": true } }}',
                    },
                  },
                ],
              },
            },
          },
          {
            name: 'Delete Data',
            value: 'deleteData',
            action: 'Delete a specific data item from a dataset',
            description: 'Remove a specific data item from a dataset while keeping the dataset intact',
            routing: {
              request: {
                method: 'DELETE',
                url: '=/datasets/{{$parameter["datasetId"]}}/data/{{$parameter["dataId"]}}',
                timeout: 300000, // 5 minutes
              },
              output: {
                postReceive: [
                  {
                    type: 'set',
                    properties: {
                      value: '={{ { "deleted": true } }}',
                    },
                  },
                ],
              },
            },
          },
        ],
        default: 'deleteDataset',
      },
      {
        displayName: 'Operation',
        name: 'operation',
        type: 'options',
        noDataExpression: true,
        displayOptions: {
          show: {
            resource: ['skill'],
          },
        },
        options: [
          {
            name: 'Apply Improvement',
            value: 'applyImprovement',
            action: 'Apply an approved skill improvement proposal',
            description: 'Apply a previously created proposal to the skill (writes the new procedure)',
            routing: {
              request: {
                method: 'POST',
                url: '/v1/remember/entry',
                headers: { 'Content-Type': 'application/json' },
                body: {
                  entry: {
                    type: 'skill_run',
                    selected_skill_id: '={{$parameter["skillName"]}}',
                    success_score: '={{$parameter["successScore"]}}',
                    feedback: -1,
                  },
                  dataset_name: '={{$parameter["skillDatasetName"]}}',
                  skill_improvement: {
                    skill_name: '={{$parameter["skillName"]}}',
                    apply: true,
                    proposal_id: '={{$parameter["proposalId"]}}',
                  },
                },
                timeout: 300000, // 5 minutes
              },
            },
          },
          {
            name: 'Get Proposal',
            value: 'getProposal',
            action: 'Get a skill improvement proposal',
            description: 'Fetch a proposal with its before/after procedures, rationale and confidence',
            routing: {
              request: {
                method: 'GET',
                url: '=/v1/proposals/{{$parameter["proposalId"]}}',
              },
            },
          },
          {
            name: 'Get Skill',
            value: 'getSkill',
            action: 'Get a skill including its procedure body',
            description: 'Fetch one skill (including its full procedure) by ID',
            routing: {
              request: {
                method: 'GET',
                url: '=/v1/skills/{{$parameter["skillId"]}}',
              },
            },
          },
          {
            name: 'Ingest Skill',
            value: 'ingestSkill',
            action: 'Ingest a skill md into a dataset',
            description: 'Ingest inline SKILL.md markdown as a dataset-scoped skill (no file upload)',
            routing: {
              request: {
                method: 'POST',
                url: '/v1/skills',
                headers: { 'Content-Type': 'application/json' },
                body: {
                  skills_text: '={{$parameter["skillsText"]}}',
                  skill_name: '={{$parameter["skillName"]}}',
                  dataset_name: '={{$parameter["skillDatasetName"]}}',
                },
                timeout: 600000, // 10 minutes
              },
            },
          },
          {
            name: 'Propose Improvement',
            value: 'proposeImprovement',
            action: 'Propose a skill improvement from a weak run',
            description: 'Record a low-scoring skill run and create a skill-improvement proposal (not applied)',
            routing: {
              request: {
                method: 'POST',
                url: '/v1/remember/entry',
                headers: { 'Content-Type': 'application/json' },
                body: {
                  entry: {
                    type: 'skill_run',
                    selected_skill_id: '={{$parameter["skillName"]}}',
                    task_text: '={{$parameter["taskText"]}}',
                    result_summary: '={{$parameter["resultSummary"]}}',
                    success_score: '={{$parameter["successScore"]}}',
                    feedback: -1,
                    candidate_skill_ids: '={{[$parameter["skillName"]]}}',
                  },
                  dataset_name: '={{$parameter["skillDatasetName"]}}',
                  skill_improvement: {
                    skill_name: '={{$parameter["skillName"]}}',
                    apply: false,
                    score_threshold: '={{$parameter["scoreThreshold"]}}',
                  },
                },
                timeout: 300000, // 5 minutes
              },
            },
          },
          {
            name: 'Review Skill',
            value: 'reviewSkill',
            action: 'Run a skill aware agentic review',
            description: 'Run an AGENTIC_COMPLETION search that loads the given skill to review a task',
            routing: {
              request: {
                method: 'POST',
                url: '/v1/search',
                headers: { 'Content-Type': 'application/json' },
                body: {
                  search_type: 'AGENTIC_COMPLETION',
                  query: '={{$parameter["reviewQuery"]}}',
                  datasets: '={{[$parameter["skillDatasetName"]]}}',
                  skills: '={{[$parameter["skillName"]]}}',
                  max_iter: '={{$parameter["reviewMaxIter"]}}',
                  top_k: '={{$parameter["reviewTopK"]}}',
                },
                timeout: 300000, // 5 minutes
              },
              output: {
                postReceive: [parseReviewScore],
              },
            },
          },
        ],
        default: 'ingestSkill',
      },
      // Add action fields
      {
        displayName: 'Dataset Name',
        name: 'datasetName',
        type: 'string',
        default: '',
        required: true,
        description: 'Name of the cognee dataset that textData will be added to',
        displayOptions: {
          show: {
            resource: ['addData'],
            operation: ['add'],
          },
        },
        routing: {
          request: {
            body: {
              datasetName: '={{$value}}',
            },
          },
        },
      },
      {
        displayName: 'Text Data',
        name: 'textData',
        type: 'string',
        typeOptions: {
          multipleValues: true,
        },
        default: [],
        required: true,
        description: 'The text content to store in the cognee dataset',
        displayOptions: {
          show: {
            resource: ['addData'],
            operation: ['add'],
          },
        },
        routing: {
          request: {
            body: {
              textData: '={{$value}}',
            },
          },
        },
      },
      // Cognify action fields
      {
        displayName: 'Datasets',
        name: 'datasets',
        type: 'string',
        typeOptions: {
          multipleValues: true,
        },
        default: [],
        required: true,
        description: 'One or more Cognee dataset names to Cognify',
        displayOptions: {
          show: {
            resource: ['cognify'],
            operation: ['cognify'],
          },
        },
        routing: {
          request: {
            body: {
              datasets: '={{$value}}',
            },
          },
        },
      },
      {
        displayName: 'Run in Background',
        name: 'runInBackground',
        type: 'boolean',
        default: false,
        description:
          'Whether to run cognify asynchronously on the Cognee Cloud API. When enabled, the request returns immediately with pipeline metadata (including pipeline_run_id) and processing continues server-side; poll GET /api/v1/datasets/status to track completion. Disable to wait synchronously, but note that the Cognee Cloud gateway closes long-running connections around the 4-minute mark, so non-trivial datasets will fail with ECONNRESET in sync mode.',
        displayOptions: {
          show: {
            resource: ['cognify'],
            operation: ['cognify'],
          },
        },
        routing: {
          request: {
            body: {
              runInBackground: '={{$value}}',
            },
          },
        },
      },
      // Search action fields
      {
        displayName: 'Search Type',
        name: 'searchType',
        type: 'options',
        options: SEARCH_COMPLETION_TYPES,
        default: 'GRAPH_COMPLETION',
        description: 'Selection of search types to query the cognee memory engine',
        displayOptions: {
          show: {
            resource: ['search'],
            operation: ['search'],
          },
        },
        routing: {
          request: {
            body: {
              searchType: '={{$value}}',
            },
          },
        },
      },
      {
        displayName: 'Datasets',
        name: 'datasets',
        type: 'string',
        typeOptions: {
          multipleValues: true,
        },
        default: [],
        required: true,
        description: 'Datasets to search in the cognee memory engine',
        displayOptions: {
          show: {
            resource: ['search'],
            operation: ['search'],
          },
        },
        routing: {
          request: {
            body: {
              datasets: '={{$value}}',
            },
          },
        },
      },
      {
        displayName: 'Query',
        name: 'query',
        type: 'string',
        default: '',
        required: true,
        description: 'The text query to search for',
        displayOptions: {
          show: {
            resource: ['search'],
            operation: ['search'],
          },
        },
        routing: {
          request: {
            body: {
              query: '={{$value}}',
            },
          },
        },
      },
      {
        displayName: 'Top K',
        name: 'topK',
        type: 'number',
        default: 10,
        description: 'Number of elements to retrieve during context creation',
        displayOptions: {
          show: {
            resource: ['search'],
            operation: ['search'],
          },
        },
        routing: {
          request: {
            body: {
              topK: '={{$value}}',
            },
          },
        },
      },
      // Recall action fields
      {
        displayName: 'Search Type',
        name: 'recallSearchType',
        type: 'options',
        options: [{ name: 'Auto (Let Cognee Route)', value: 'AUTO' }, ...SEARCH_COMPLETION_TYPES],
        default: 'AUTO',
        description: 'Search strategy for recall. Auto sends search_type null so cognee routes the query for you.',
        displayOptions: {
          show: {
            resource: ['recall'],
            operation: ['recall'],
          },
        },
      },
      {
        displayName: 'Query',
        name: 'recallQuery',
        type: 'string',
        default: '',
        required: true,
        description: 'The text query to recall from cognee memory',
        displayOptions: {
          show: {
            resource: ['recall'],
            operation: ['recall'],
          },
        },
      },
      {
        displayName: 'Datasets',
        name: 'recallDatasets',
        type: 'string',
        typeOptions: {
          multipleValues: true,
        },
        default: [],
        description: 'Dataset names to recall from. Leave empty to search all datasets you can read.',
        displayOptions: {
          show: {
            resource: ['recall'],
            operation: ['recall'],
          },
        },
      },
      {
        displayName: 'Session ID',
        name: 'recallSessionId',
        type: 'string',
        default: '',
        description: 'Session whose cached QA/trace entries should be recalled. Leave empty to search the graph only.',
        displayOptions: {
          show: {
            resource: ['recall'],
            operation: ['recall'],
          },
        },
      },
      {
        displayName: 'Node Names',
        name: 'recallNodeName',
        type: 'string',
        typeOptions: {
          multipleValues: true,
        },
        default: [],
        description: 'Restrict recall to these node sets (the node_set tags passed to Remember/Add). Leave empty to search all nodes.',
        displayOptions: {
          show: {
            resource: ['recall'],
            operation: ['recall'],
          },
        },
      },
      {
        displayName: 'Scope',
        name: 'recallScope',
        type: 'options',
        options: [
          { name: 'All', value: 'all' },
          { name: 'Auto', value: 'auto' },
          { name: 'Graph', value: 'graph' },
          { name: 'Graph Context', value: 'graph_context' },
          { name: 'Session', value: 'session' },
          { name: 'Trace', value: 'trace' },
        ],
        default: 'auto',
        description: 'Which memory sources to include. Auto uses the session first when a Session ID is set, else the graph.',
        displayOptions: {
          show: {
            resource: ['recall'],
            operation: ['recall'],
          },
        },
      },
      {
        displayName: 'Top K',
        name: 'recallTopK',
        type: 'number',
        default: 15,
        description: 'Number of elements to retrieve during context creation. Set 0 or empty to use the recall backend default of 15 (Search defaults to 10).',
        displayOptions: {
          show: {
            resource: ['recall'],
            operation: ['recall'],
          },
        },
      },
      // Remember action fields
      {
        displayName: 'Dataset Name',
        name: 'rememberDatasetName',
        type: 'string',
        default: '',
        required: true,
        description: 'Name of the target dataset (created if it does not exist)',
        displayOptions: {
          show: {
            resource: ['remember'],
            operation: ['remember'],
          },
        },
      },
      {
        displayName: 'Dataset ID',
        name: 'rememberDatasetId',
        type: 'string',
        default: '',
        description: 'UUID of an existing dataset to target instead of resolving by name. Leave empty to use the Dataset Name.',
        displayOptions: {
          show: {
            resource: ['remember'],
            operation: ['remember'],
          },
        },
      },
      {
        displayName: 'Text',
        name: 'rememberText',
        type: 'string',
        typeOptions: {
          rows: 6,
        },
        default: '',
        required: true,
        description: 'Text to remember. It is ingested as an uploaded .txt file and cognified in one call.',
        displayOptions: {
          show: {
            resource: ['remember'],
            operation: ['remember'],
          },
        },
      },
      {
        displayName: 'Session ID',
        name: 'rememberSessionId',
        type: 'string',
        default: '',
        description: 'Session to attribute this memory to (e.g. claude-code-1718000000). Leave empty for a direct add+cognify.',
        displayOptions: {
          show: {
            resource: ['remember'],
            operation: ['remember'],
          },
        },
      },
      {
        displayName: 'Node Sets',
        name: 'rememberNodeSet',
        type: 'string',
        typeOptions: {
          multipleValues: true,
        },
        default: [],
        description: 'Tags the ingested data with these node sets so recall/search can later be restricted to them',
        displayOptions: {
          show: {
            resource: ['remember'],
            operation: ['remember'],
          },
        },
      },
      {
        displayName: 'Run in Background',
        name: 'rememberRunInBackground',
        type: 'boolean',
        default: true,
        description: 'Whether the server should cognify asynchronously. When enabled the request returns as soon as the work is enqueued. Disable to wait synchronously, but note that the Cognee Cloud gateway closes long-running connections around the 4-minute mark, so non-trivial texts will fail with ECONNRESET in sync mode.',
        displayOptions: {
          show: {
            resource: ['remember'],
            operation: ['remember'],
          },
        },
      },
      // Delete action fields
      {
        displayName: 'Dataset ID',
        name: 'datasetId',
        type: 'string',
        default: '',
        required: true,
        description: 'The unique identifier (UUID) of the dataset',
        displayOptions: {
          show: {
            resource: ['delete'],
          },
        },
      },
      {
        displayName: 'Data ID',
        name: 'dataId',
        type: 'string',
        default: '',
        required: true,
        description: 'The unique identifier (UUID) of the data item to delete',
        displayOptions: {
          show: {
            resource: ['delete'],
            operation: ['deleteData'],
          },
        },
      },
      // Skill action fields
      {
        displayName: 'Skill Name',
        name: 'skillName',
        type: 'string',
        default: '',
        required: true,
        description: 'Name/slug of the skill (e.g. "code-review")',
        displayOptions: {
          show: {
            resource: ['skill'],
            operation: ['ingestSkill', 'reviewSkill', 'proposeImprovement', 'applyImprovement'],
          },
        },
      },
      {
        displayName: 'Dataset Name',
        name: 'skillDatasetName',
        type: 'string',
        default: '',
        required: true,
        description: 'Name of the dataset the skill lives in (created if needed on ingest)',
        displayOptions: {
          show: {
            resource: ['skill'],
            operation: ['ingestSkill', 'reviewSkill', 'proposeImprovement', 'applyImprovement'],
          },
        },
      },
      {
        displayName: 'Skill Markdown',
        name: 'skillsText',
        type: 'string',
        typeOptions: {
          rows: 8,
        },
        default: '',
        required: true,
        description: 'The full SKILL.md markdown body to ingest (frontmatter optional)',
        displayOptions: {
          show: {
            resource: ['skill'],
            operation: ['ingestSkill'],
          },
        },
      },
      {
        displayName: 'Query',
        name: 'reviewQuery',
        type: 'string',
        typeOptions: {
          rows: 4,
        },
        default: '',
        required: true,
        description: 'The review task to run with the skill loaded (agentic completion)',
        displayOptions: {
          show: {
            resource: ['skill'],
            operation: ['reviewSkill'],
          },
        },
      },
      {
        displayName: 'Max Iterations',
        name: 'reviewMaxIter',
        type: 'number',
        default: 6,
        description: 'Maximum agentic tool-call iterations before forcing a final answer',
        displayOptions: {
          show: {
            resource: ['skill'],
            operation: ['reviewSkill'],
          },
        },
      },
      {
        displayName: 'Top K',
        name: 'reviewTopK',
        type: 'number',
        default: 15,
        description: 'Number of elements to retrieve during context creation',
        displayOptions: {
          show: {
            resource: ['skill'],
            operation: ['reviewSkill'],
          },
        },
      },
      {
        displayName: 'Task Text',
        name: 'taskText',
        type: 'string',
        typeOptions: {
          rows: 3,
        },
        default: '',
        description: 'The task that was attempted (recorded on the skill run)',
        displayOptions: {
          show: {
            resource: ['skill'],
            operation: ['proposeImprovement'],
          },
        },
      },
      {
        displayName: 'Result Summary',
        name: 'resultSummary',
        type: 'string',
        typeOptions: {
          rows: 3,
        },
        default: '',
        description: 'Summary of what the weak run produced / what instruction was missing',
        displayOptions: {
          show: {
            resource: ['skill'],
            operation: ['proposeImprovement'],
          },
        },
      },
      {
        displayName: 'Success Score',
        name: 'successScore',
        type: 'number',
        typeOptions: {
          minValue: 0,
          maxValue: 1,
          numberPrecision: 2,
        },
        default: 0,
        description: 'Evaluator score for the run in range [0, 1]',
        displayOptions: {
          show: {
            resource: ['skill'],
            operation: ['proposeImprovement', 'applyImprovement'],
          },
        },
      },
      {
        displayName: 'Score Threshold',
        name: 'scoreThreshold',
        type: 'number',
        typeOptions: {
          minValue: 0,
          maxValue: 1,
          numberPrecision: 2,
        },
        default: 0.9,
        description: 'Minimum score required to skip improvement (runs at or below trigger a proposal)',
        displayOptions: {
          show: {
            resource: ['skill'],
            operation: ['proposeImprovement'],
          },
        },
      },
      {
        displayName: 'Proposal ID',
        name: 'proposalId',
        type: 'string',
        default: '',
        required: true,
        description: 'The proposal_id returned by Propose Improvement',
        displayOptions: {
          show: {
            resource: ['skill'],
            operation: ['applyImprovement', 'getProposal'],
          },
        },
      },
      {
        displayName: 'Skill ID',
        name: 'skillId',
        type: 'string',
        default: '',
        required: true,
        description: 'The skill identifier returned by Get Skill / list',
        displayOptions: {
          show: {
            resource: ['skill'],
            operation: ['getSkill'],
          },
        },
      },
      {
        displayName: 'Dataset ID',
        name: 'getDatasetId',
        type: 'string',
        default: '',
        required: true,
        description: 'UUID of the dataset the skill/proposal is scoped to (returned by Ingest Skill)',
        displayOptions: {
          show: {
            resource: ['skill'],
            operation: ['getSkill', 'getProposal'],
          },
        },
        routing: {
          request: {
            qs: {
              dataset_id: '={{$value}}',
            },
          },
        },
      },
    ],
  };
}
