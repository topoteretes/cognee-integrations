import type {
  IExecuteSingleFunctions,
  IN8nHttpFullResponse,
  INodeExecutionData,
  INodeType,
  INodeTypeDescription,
} from 'n8n-workflow';
import { NodeConnectionTypes } from 'n8n-workflow';

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

export class Cognee implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'Cognee',
    name: 'cognee',
    icon: 'file:cognee.svg',
    group: ['transform'],
    usableAsTool: true,
    version: 1,
    subtitle: '={{$parameter["resource"] + ": " + $parameter["operation"]}}',
    description: 'Remember and recall from Cognee memory, add text data to datasets, build a knowledge graph, search Cognee memory, manage datasets, and run the self-improving skill loop',
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
      // Remember operation
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
            action: 'Store data in cognee memory',
            description: 'Store data in Cognee memory. Without a session ID the data is ingested into the permanent knowledge graph. With a session ID it is cached for fast session-scoped retrieval.',
            routing: {
              request: {
                method: 'POST',
                url: '/v1/remember',
                headers: {
                  'Content-Type': 'application/json',
                },
                timeout: 600000, // 10 minutes — cognify can be slow
              },
            },
          },
        ],
        default: 'remember',
      },
      // Remember action fields
      {
        displayName: 'Data',
        name: 'rememberData',
        type: 'string',
        typeOptions: {
          rows: 4,
        },
        default: '',
        required: true,
        description: 'The text content to store in Cognee memory',
        displayOptions: {
          show: {
            resource: ['remember'],
            operation: ['remember'],
          },
        },
        routing: {
          request: {
            body: {
              data: '={{$value}}',
            },
          },
        },
      },
      {
        displayName: 'Dataset Name',
        name: 'rememberDatasetName',
        type: 'string',
        default: 'main_dataset',
        required: true,
        description: 'Name of the Cognee dataset to store the data in',
        displayOptions: {
          show: {
            resource: ['remember'],
            operation: ['remember'],
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
        displayName: 'Session ID',
        name: 'rememberSessionId',
        type: 'string',
        default: '',
        description: 'Optional session identifier. When provided, data is stored in the fast session cache instead of the permanent knowledge graph.',
        displayOptions: {
          show: {
            resource: ['remember'],
            operation: ['remember'],
          },
        },
        routing: {
          request: {
            body: {
              sessionId: '={{$value || undefined}}',
            },
          },
        },
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
        options: [
          { name: 'GraphCompletion', value: 'GRAPH_COMPLETION' },
          { name: 'ChainOfThought', value: 'GRAPH_COMPLETION_COT' },
          { name: 'RagCompletion', value: 'RAG_COMPLETION' },
        ],
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
