import type {
  IExecuteSingleFunctions,
  IHttpRequestOptions,
  IN8nHttpFullResponse,
  INodeExecutionData,
  INodeType,
  INodeTypeDescription,
} from 'n8n-workflow';
import { NodeConnectionTypes, NodeOperationError } from 'n8n-workflow';
import { parseReviewJson, unwrapSearchAnswer } from './helpers';
import {
  buildTextIngestionParts,
  createMultipartBoundary,
  encodeMultipart,
  multipartContentType,
} from './multipart';

/**
 * preSend hook for Add Data: POST /v1/add only accepts content as uploaded
 * files, so each text item is encoded as a multipart file part together with
 * the dataset and optional fields. Replaces the JSON body n8n would send.
 */
export async function buildAddDataBody(
  this: IExecuteSingleFunctions,
  requestOptions: IHttpRequestOptions,
): Promise<IHttpRequestOptions> {
  const rawText = this.getNodeParameter('textData', []) as string | string[];
  const texts = Array.isArray(rawText) ? rawText : [rawText];
  const additional = this.getNodeParameter('addAdditionalFields', {}) as {
    nodeSet?: string[];
    runInBackground?: boolean;
  };

  const parts = buildTextIngestionParts({
    texts,
    datasetName: this.getNodeParameter('datasetName') as string,
    nodeSet: additional.nodeSet,
    runInBackground: additional.runInBackground,
  });

  if (!parts.some((part) => 'filename' in part)) {
    throw new NodeOperationError(this.getNode(), 'Provide at least one non-empty Text Data item');
  }

  const boundary = createMultipartBoundary();
  requestOptions.body = encodeMultipart(parts, boundary);
  requestOptions.headers = {
    ...requestOptions.headers,
    'Content-Type': multipartContentType(boundary),
  };
  return requestOptions;
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
    icon: { light: 'file:cognee.svg', dark: 'file:cognee.dark.svg' },
    group: ['transform'],
    usableAsTool: true,
    version: 1,
    subtitle: '={{$parameter["resource"] + ": " + $parameter["operation"]}}',
    description: 'Add text data to a Cognee dataset, build a knowledge graph, search Cognee memory, delete datasets or data items, and run the self-improving skill loop (ingest, review, propose, apply) via the Cognee /api/v1 API',
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
                url: '/v1/add',
                timeout: 300000, // 5 minutes
              },
              send: {
                preSend: [buildAddDataBody],
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
                url: '/v1/cognify',
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
                url: '/v1/search',
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
                url: '=/v1/datasets/{{$parameter["datasetId"]}}',
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
                url: '=/v1/datasets/{{$parameter["datasetId"]}}/data/{{$parameter["dataId"]}}',
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
        description: 'Name of the Cognee dataset the text will be added to (created if it does not exist)',
        displayOptions: {
          show: {
            resource: ['addData'],
            operation: ['add'],
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
        description: 'The text content to store in the Cognee dataset. Each item is uploaded as its own text file.',
        displayOptions: {
          show: {
            resource: ['addData'],
            operation: ['add'],
          },
        },
      },
      {
        displayName: 'Additional Fields',
        name: 'addAdditionalFields',
        type: 'collection',
        placeholder: 'Add Field',
        default: {},
        displayOptions: {
          show: {
            resource: ['addData'],
            operation: ['add'],
          },
        },
        options: [
          {
            displayName: 'Node Set',
            name: 'nodeSet',
            type: 'string',
            typeOptions: {
              multipleValues: true,
            },
            default: [],
            description:
              'Tag the ingested data with named node sets (e.g. per-agent or per-project groups). Search and Recall can later be restricted to them.',
          },
          {
            displayName: 'Run in Background',
            name: 'runInBackground',
            type: 'boolean',
            default: false,
            description:
              'Whether to return immediately with a pipeline_run_id while ingestion continues server-side. Poll GET /api/v1/datasets/status to track completion.',
          },
        ],
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
              run_in_background: '={{$value}}',
            },
          },
        },
      },
      {
        displayName: 'Additional Options',
        name: 'cognifyAdditionalOptions',
        type: 'collection',
        placeholder: 'Add Option',
        default: {},
        displayOptions: {
          show: {
            resource: ['cognify'],
            operation: ['cognify'],
          },
        },
        options: [
          {
            displayName: 'Chunk Size',
            name: 'chunkSize',
            type: 'number',
            default: 4096,
            description:
              'Maximum tokens per text chunk during graph building. Larger chunks give more context per extraction; smaller chunks give finer-grained extraction at higher LLM cost.',
            routing: {
              request: {
                body: {
                  chunk_size: '={{$value}}',
                },
              },
            },
          },
          {
            displayName: 'Custom Prompt',
            name: 'customPrompt',
            type: 'string',
            typeOptions: {
              rows: 4,
            },
            default: '',
            description:
              'Replaces the default entity-extraction prompt. Use it to steer which entities and relationships get extracted.',
            routing: {
              request: {
                body: {
                  custom_prompt: '={{$value}}',
                },
              },
            },
          },
          {
            displayName: 'Dataset IDs',
            name: 'datasetIds',
            type: 'string',
            typeOptions: {
              multipleValues: true,
            },
            default: [],
            description:
              'Dataset UUIDs to cognify. Required for datasets shared with you; names only resolve to datasets you own.',
            routing: {
              request: {
                body: {
                  dataset_ids: '={{$value}}',
                },
              },
            },
          },
          {
            displayName: 'Ontology Keys',
            name: 'ontologyKeys',
            type: 'string',
            typeOptions: {
              multipleValues: true,
            },
            default: [],
            description:
              'Keys of previously uploaded ontologies (see /api/v1/ontologies) used to ground entity extraction',
            routing: {
              request: {
                body: {
                  ontology_key: '={{$value}}',
                },
              },
            },
          },
        ],
      },
      // Search action fields
      {
        displayName: 'Search Type',
        name: 'searchType',
        type: 'options',
        options: [
          { name: 'Agentic Completion', value: 'AGENTIC_COMPLETION' },
          { name: 'Chain of Thought', value: 'GRAPH_COMPLETION_COT' },
          { name: 'Chunks', value: 'CHUNKS' },
          { name: 'Chunks (Lexical)', value: 'CHUNKS_LEXICAL' },
          { name: 'Code', value: 'CODE' },
          { name: 'Coding Rules', value: 'CODING_RULES' },
          { name: 'Cypher', value: 'CYPHER' },
          { name: 'Feeling Lucky', value: 'FEELING_LUCKY' },
          { name: 'Graph Completion', value: 'GRAPH_COMPLETION' },
          { name: 'Graph Completion (Context Extension)', value: 'GRAPH_COMPLETION_CONTEXT_EXTENSION' },
          { name: 'Graph Completion (Decomposition)', value: 'GRAPH_COMPLETION_DECOMPOSITION' },
          { name: 'Graph Report', value: 'GRAPH_REPORT' },
          { name: 'Graph Summary Completion', value: 'GRAPH_SUMMARY_COMPLETION' },
          { name: 'Hybrid Completion', value: 'HYBRID_COMPLETION' },
          { name: 'Natural Language', value: 'NATURAL_LANGUAGE' },
          { name: 'RAG Completion', value: 'RAG_COMPLETION' },
          { name: 'Summaries', value: 'SUMMARIES' },
          { name: 'Temporal', value: 'TEMPORAL' },
          { name: 'Triplet Completion', value: 'TRIPLET_COMPLETION' },
        ],
        default: 'GRAPH_COMPLETION',
        description:
          'Retrieval strategy. Completion types return an LLM answer grounded in memory; Chunks, Summaries and Code return raw retrieval results.',
        displayOptions: {
          show: {
            resource: ['search'],
            operation: ['search'],
          },
        },
        routing: {
          request: {
            body: {
              search_type: '={{$value}}',
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
        description: 'Dataset names to search. Names only resolve to datasets you own; use Dataset IDs for shared datasets.',
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
              top_k: '={{$value}}',
            },
          },
        },
      },
      {
        displayName: 'Additional Options',
        name: 'searchAdditionalOptions',
        type: 'collection',
        placeholder: 'Add Option',
        default: {},
        displayOptions: {
          show: {
            resource: ['search'],
            operation: ['search'],
          },
        },
        options: [
          {
            displayName: 'Dataset IDs',
            name: 'datasetIds',
            type: 'string',
            typeOptions: {
              multipleValues: true,
            },
            default: [],
            description:
              'Dataset UUIDs to search. Required for datasets shared with you. When set, the Datasets names are ignored.',
            routing: {
              request: {
                body: {
                  dataset_ids: '={{$value}}',
                },
              },
            },
          },
          {
            displayName: 'Include References',
            name: 'includeReferences',
            type: 'boolean',
            default: false,
            description: 'Whether to attach source/provenance references to completion results',
            routing: {
              request: {
                body: {
                  include_references: '={{$value}}',
                },
              },
            },
          },
          {
            displayName: 'Node Sets',
            name: 'nodeName',
            type: 'string',
            typeOptions: {
              multipleValues: true,
            },
            default: [],
            description:
              'Restrict results to these node sets (the Node Set values used when adding data)',
            routing: {
              request: {
                body: {
                  node_name: '={{$value}}',
                },
              },
            },
          },
          {
            displayName: 'Only Context',
            name: 'onlyContext',
            type: 'boolean',
            default: false,
            description:
              'Whether to return only the retrieved context Cognee would send to the LLM, skipping the completion step',
            routing: {
              request: {
                body: {
                  only_context: '={{$value}}',
                },
              },
            },
          },
          {
            displayName: 'Session ID',
            name: 'sessionId',
            type: 'string',
            default: '',
            description:
              'Session whose history and guidance feed the completion. Omit to use the default session.',
            routing: {
              request: {
                body: {
                  session_id: '={{$value}}',
                },
              },
            },
          },
          {
            displayName: 'System Prompt',
            name: 'systemPrompt',
            type: 'string',
            typeOptions: {
              rows: 3,
            },
            default: '',
            description: 'System prompt used by completion-type searches',
            routing: {
              request: {
                body: {
                  system_prompt: '={{$value}}',
                },
              },
            },
          },
          {
            displayName: 'Verbose',
            name: 'verbose',
            type: 'boolean',
            default: false,
            description:
              'Whether to return detailed result information, including the graph representation when available',
            routing: {
              request: {
                body: {
                  verbose: '={{$value}}',
                },
              },
            },
          },
        ],
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
