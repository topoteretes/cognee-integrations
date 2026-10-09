import type {
	INodeType,
	INodeTypeDescription,
	ISupplyDataFunctions,
	JsonObject,
	Logger,
	SupplyData,
} from 'n8n-workflow';
import { NodeApiError, NodeConnectionTypes, NodeOperationError } from 'n8n-workflow';
import { supplyMemory } from '@n8n/ai-node-sdk';

import { DEFAULT_DATASET_NAME } from '../Cognee/payloads';
import { CogneeAgentMemory, CogneeChatHistory, MAX_SESSION_PAIRS } from './memory';
import type {
	CogneeRequest,
	CogneeRequestOptions,
	PromotionMode,
	PromotionResult,
	PromotionState,
	RecallSettings,
} from './memory';
import { DEFAULT_CONTEXT_CHARS } from './recallContext';

type MemoryOptions = {
	datasetName?: string;
	datasetId?: string;
	windowSize?: number;
	recallContext?: boolean;
	recallScope?: string[];
	recallDatasets?: string;
	recallTopK?: number;
	maxContextChars?: number;
	injectAs?: 'system' | 'user';
	promoteMode?: PromotionMode;
	promoteEveryHours?: number;
};

export const DEFAULT_WINDOW_SIZE = 5;
export const DEFAULT_RECALL_TOP_K = 5;
export const DEFAULT_PROMOTE_HOURS = 24;
export const RECALL_SEARCH_TYPE = 'HYBRID_COMPLETION';
export const DEFAULT_RECALL_QUERY = '={{ $json.chatInput }}';
/** Key under the node's static data holding the promotion record. */
export const STATIC_DATA_KEY = 'cogneeMemory';

/**
 * A finite number from an option value, which an expression can turn into
 * anything: undefined, a numeric string, NaN, Infinity. Out-of-range values
 * are clamped, non-numbers fall back to the default.
 */
export function finiteNumber(
	value: unknown,
	bounds: { default: number; min: number; max?: number; integer?: boolean },
): number {
	let n =
		typeof value === 'number' ? value : typeof value === 'string' ? Number(value.trim()) : NaN;
	if (!Number.isFinite(n)) n = bounds.default;
	if (bounds.integer) n = Math.floor(n);
	n = Math.max(bounds.min, n);
	if (bounds.max !== undefined) n = Math.min(bounds.max, n);
	return n;
}

/**
 * AI Agent memory sub-node: long-term memory in Cognee. Stores the
 * conversation in a Cognee session, recalls graph context for every question
 * and promotes the sessions into the knowledge graph once per interval, so a
 * later conversation under a new session ID can recall what was said.
 */
export class CogneeMemory implements INodeType {
	description: INodeTypeDescription = {
		displayName: 'Cognee Memory',
		name: 'cogneeMemory',
		// Shared with the Cognee action node; the loader resolves icon paths
		// relative to this file and only requires them to stay inside the package.
		icon: { light: 'file:../Cognee/cognee.svg', dark: 'file:../Cognee/cognee.dark.svg' },
		group: ['transform'],
		version: [1],
		subtitle: 'Long-term memory in Cognee',
		description:
			'Give the AI Agent long-term memory: the conversation is stored in a Cognee session, relevant knowledge is recalled for every question, and sessions are promoted into the knowledge graph so later conversations remember',
		defaults: {
			name: 'Cognee Memory',
		},
		codex: {
			// Matches n8n's own memory sub-nodes (Postgres / Redis Chat Memory),
			// so this lands in the same place in the node picker.
			categories: ['AI'],
			subcategories: {
				AI: ['Memory'],
				Memory: ['Other memories'],
			},
			resources: {
				primaryDocumentation: [
					{
						url: 'https://github.com/topoteretes/cognee-integrations/tree/main/integrations/n8n#sub-node-cognee-memory',
					},
				],
			},
		},
		inputs: [],
		outputs: [NodeConnectionTypes.AiMemory],
		outputNames: ['Memory'],
		credentials: [
			{
				name: 'cogneeApi',
				required: true,
			},
		],
		properties: [
			{
				displayName: 'Session ID',
				name: 'sessionId',
				type: 'string',
				default: '={{ $json.sessionId }}',
				required: true,
				description:
					'Cognee session the conversation is stored under. Each Q&A turn becomes a session entry; the same value works with the Cognee node (Recall with Session ID, Session → Get). Sessions are keyed per Cognee user, not per dataset, so keep this globally unique — namespace it yourself if two workflows could pick the same value.',
				placeholder: 'user-123',
			},
			{
				displayName: 'Recall Query',
				name: 'recallQuery',
				type: 'string',
				default: DEFAULT_RECALL_QUERY,
				description:
					'The question to recall long-term memory for, resolved from the item entering the agent. The default reads the Chat Trigger message. When it resolves to nothing, recall is skipped for that turn. Turn recall off entirely under Options.',
				placeholder: '={{ $json.chatInput }}',
			},
			{
				displayName: 'Options',
				name: 'options',
				placeholder: 'Add Option',
				description: 'Additional options for memory management',
				type: 'collection',
				default: {},
				options: [
					{
						displayName: 'Dataset ID',
						name: 'datasetId',
						type: 'string',
						default: '',
						description:
							'Attribute the session by dataset UUID instead of by name. Required for a dataset shared with you, because a name only resolves among datasets you own. Takes precedence over Dataset Name.',
					},
					{
						displayName: 'Dataset Name',
						name: 'datasetName',
						type: 'string',
						default: DEFAULT_DATASET_NAME,
						description:
							'Dataset this session is attributed to, recorded on the session the first time it is written, and the dataset its turns are promoted into. Later writes do not move an existing session, and sessions are keyed per user rather than per dataset, so reusing one Session ID with different datasets mixes one history rather than splitting it.',
					},
					{
						displayName: 'Inject Context As',
						name: 'injectAs',
						type: 'options',
						options: [
							{ name: 'System Message', value: 'system' },
							{ name: 'User Message', value: 'user' },
						],
						default: 'system',
						description:
							'Role of the message that carries the recalled context. It is placed after the history window, right before the incoming question. Switch to User Message for chat models that reject a system message that is not the first message.',
					},
					{
						displayName: 'Max Context Characters',
						name: 'maxContextChars',
						type: 'number',
						default: DEFAULT_CONTEXT_CHARS,
						description:
							'Upper bound on the recalled context handed to the agent per turn. Longer context is cut at a line break.',
						typeOptions: {
							minValue: 200,
						},
					},
					{
						displayName: 'Promote Every (Hours)',
						name: 'promoteEveryHours',
						type: 'number',
						default: DEFAULT_PROMOTE_HOURS,
						description:
							'Minimum time between promotions when Promote To Graph is Every N Hours. Promotion runs at the end of the first execution after this much time has passed, covering every session written since the last run.',
						typeOptions: {
							minValue: 0.1,
						},
					},
					{
						displayName: 'Promote To Graph',
						name: 'promoteMode',
						type: 'options',
						options: [
							{
								name: 'After Each Execution',
								value: 'eachExecution',
								description:
									'Promote this session at the end of every execution. Immediate cross-conversation memory at the cost of one improve run per message.',
							},
							{
								name: 'Every N Hours',
								value: 'interval',
								description:
									'Promote all sessions written since the last run, at most once per Promote Every (Hours). The time of the last run is kept in the workflow static data, which n8n only saves for production executions, so manual test runs promote every time.',
							},
							{
								name: 'Never',
								value: 'never',
								description:
									'Keep turns in the session only. Nothing becomes graph knowledge unless a separate workflow promotes it (Cognee node → Memory → Improve).',
							},
						],
						default: 'interval',
						description:
							'When the sessions this node writes are promoted into the knowledge graph (POST /api/v1/improve), which is what lets a later conversation recall them. Improve is idempotent per session, so a run only processes turns added since the previous one.',
					},
					{
						displayName: 'Recall Context',
						name: 'recallContext',
						type: 'boolean',
						default: true,
						description:
							'Whether to recall relevant knowledge from Cognee for every incoming question and hand it to the agent next to the chat history. Uses a hybrid completion search with only the retrieved context returned.',
					},
					{
						displayName: 'Recall Datasets',
						name: 'recallDatasets',
						type: 'string',
						default: '',
						placeholder: 'support_docs, company_wiki',
						description:
							'Comma-separated dataset names to recall from. Leave empty to recall from the Dataset Name (or Dataset ID) this memory writes to.',
					},
					{
						displayName: 'Recall Scope',
						name: 'recallScope',
						type: 'multiOptions',
						options: [
							{ name: 'Code', value: 'code' },
							{ name: 'Graph', value: 'graph' },
							{ name: 'Session', value: 'session' },
							{ name: 'Session Context', value: 'session_context' },
							{ name: 'Tools', value: 'tools' },
							{ name: 'Trace', value: 'trace' },
						],
						default: ['graph'],
						description:
							'Memory layers recall reads. Graph is the knowledge graph, including promoted sessions. Session scopes read this session, which the history window already carries, so adding them mostly duplicates it.',
					},
					{
						displayName: 'Recall Top K',
						name: 'recallTopK',
						type: 'number',
						default: DEFAULT_RECALL_TOP_K,
						description: 'Number of graph hits the server retrieves before rendering the context',
						typeOptions: {
							minValue: 1,
						},
					},
					{
						displayName: 'Window Size',
						name: 'windowSize',
						type: 'number',
						default: DEFAULT_WINDOW_SIZE,
						description:
							'Number of recent question/answer pairs of this session loaded into the agent context. Older turns are reachable through recall once promoted.',
						typeOptions: {
							minValue: 1,
							maxValue: MAX_SESSION_PAIRS,
						},
					},
				],
			},
		],
	};

	async supplyData(this: ISupplyDataFunctions, itemIndex: number): Promise<SupplyData> {
		const sessionId = String(this.getNodeParameter('sessionId', itemIndex, '') ?? '').trim();
		if (!sessionId) {
			throw new NodeOperationError(this.getNode(), 'Session ID is required', { itemIndex });
		}
		const options = this.getNodeParameter('options', itemIndex, {}) as MemoryOptions;
		const datasetName = (options.datasetName ?? '').trim() || DEFAULT_DATASET_NAME;
		const datasetId = (options.datasetId ?? '').trim() || undefined;
		// The server returns no more than MAX_SESSION_PAIRS pairs.
		const windowSize = finiteNumber(options.windowSize, {
			default: DEFAULT_WINDOW_SIZE,
			min: 1,
			max: MAX_SESSION_PAIRS,
			integer: true,
		});

		const credentials = await this.getCredentials('cogneeApi');
		const baseUrl = String(credentials.baseUrl ?? '').replace(/\/+$/, '');
		if (!baseUrl) {
			throw new NodeOperationError(this.getNode(), 'The Cognee API credential has no Base URL', {
				itemIndex,
			});
		}

		// The credential's authenticate block adds the X-Api-Key header.
		const request: CogneeRequest = async (requestOptions: CogneeRequestOptions) => {
			try {
				return await this.helpers.httpRequestWithAuthentication.call(this, 'cogneeApi', {
					method: requestOptions.method,
					url: `${baseUrl}/api${requestOptions.url}`,
					body: requestOptions.body,
					headers: { Accept: 'application/json' },
					json: true,
					// Both sit on the agent's critical path, so neither waits long.
					// A write also indexes the entry, so it gets more room than a read.
					timeout: requestOptions.method === 'GET' ? 60_000 : 120_000,
				});
			} catch (error) {
				// NodeApiError already normalises axios, fetch and n8n error shapes
				// into `httpCode`, so let it do the work rather than probing here.
				const apiError = new NodeApiError(this.getNode(), error as JsonObject);
				if (requestOptions.allowNotFound && Number(apiError.httpCode) === 404) return undefined;
				throw apiError;
			}
		};

		const meta: Record<string, unknown> = { node: this.getNode().name };
		const warn = (message: string) => this.logger.warn(message, meta);
		const debug = (message: string) => this.logger.debug(message, meta);

		const recallQuery = String(this.getNodeParameter('recallQuery', itemIndex, '') ?? '');
		const recall = resolveRecallSettings(options, recallQuery, { datasetName, datasetId });
		const promoteMode: PromotionMode = options.promoteMode ?? 'interval';
		const hours = finiteNumber(options.promoteEveryHours, {
			default: DEFAULT_PROMOTE_HOURS,
			min: 0.01,
		});
		// The node's static data persists across production executions; the
		// promotion record is mutated in place and saved by n8n after the run.
		const staticData = this.getWorkflowStaticData('node');
		const state = (staticData[STATIC_DATA_KEY] ??= {}) as PromotionState;

		const chatHistory = new CogneeChatHistory({ sessionId, datasetName, datasetId, request });
		const memory = new CogneeAgentMemory({
			chatHistory,
			sessionId,
			windowSize,
			request,
			recall,
			promotion: {
				policy: { mode: promoteMode, intervalMs: hours * 3_600_000 },
				target: { datasetName, datasetId },
				state,
			},
			warn,
			debug,
		});

		return supplyMemory(this, memory, {
			closeFunction: async () => {
				logPromotion(this.logger, meta, await memory.promoteIfDue());
			},
		});
	}
}

/**
 * Recall settings from the options collection, or undefined when recall is
 * off or the query resolved to nothing. Defaults apply when the user never
 * opened the option, so a freshly added node recalls from its own dataset.
 */
export function resolveRecallSettings(
	options: MemoryOptions,
	recallQuery: string,
	target: { datasetName: string; datasetId?: string },
): RecallSettings | undefined {
	if (options.recallContext === false) return undefined;
	const query = String(recallQuery ?? '').trim();
	if (!query) return undefined;

	const datasets = String(options.recallDatasets ?? '')
		.split(',')
		.map((name) => name.trim())
		.filter((name) => name.length > 0);
	const useTarget = datasets.length === 0;
	const scope =
		Array.isArray(options.recallScope) && options.recallScope.length
			? options.recallScope
			: ['graph'];
	const topK = finiteNumber(options.recallTopK, {
		default: DEFAULT_RECALL_TOP_K,
		min: 1,
		integer: true,
	});
	const maxChars = finiteNumber(options.maxContextChars, {
		default: DEFAULT_CONTEXT_CHARS,
		min: 200,
		integer: true,
	});

	return {
		query,
		searchType: RECALL_SEARCH_TYPE,
		scope,
		datasets: useTarget && !target.datasetId ? [target.datasetName] : datasets,
		datasetIds: useTarget && target.datasetId ? [target.datasetId] : undefined,
		topK,
		maxChars,
		role: options.injectAs === 'user' ? 'user' : 'system',
	};
}

/** One log line per promotion batch, so the dev console shows what left the queue. */
export function logPromotion(
	logger: Pick<Logger, 'info' | 'debug'>,
	meta: Record<string, unknown>,
	result: PromotionResult,
): void {
	if (result.outcome === 'skipped') {
		logger.debug(`Cognee Memory: promotion skipped (${result.reason})`, meta);
		return;
	}
	for (const batch of result.batches) {
		const dataset = batch.target.datasetId ?? batch.target.datasetName;
		const n = `${batch.sessionIds.length} session(s)`;
		if (batch.status === 'submitted') {
			logger.info(`Cognee Memory: promoted ${n} into "${dataset}"`, {
				...meta,
				sessionIds: batch.sessionIds,
			});
		} else if (batch.status === 'busy') {
			logger.info(
				`Cognee Memory: server busy, ${n} for "${dataset}" stay queued for the next execution`,
				{
					...meta,
					sessionIds: batch.sessionIds,
				},
			);
		}
		// A failed batch already produced a warning from the memory.
	}
}
