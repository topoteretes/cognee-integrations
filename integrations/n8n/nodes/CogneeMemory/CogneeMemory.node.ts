import type {
	INodeType,
	INodeTypeDescription,
	ISupplyDataFunctions,
	JsonObject,
	SupplyData,
} from 'n8n-workflow';
import { NodeApiError, NodeConnectionTypes, NodeOperationError } from 'n8n-workflow';
import { WindowedChatMemory, supplyMemory } from '@n8n/ai-node-sdk';

import { DEFAULT_DATASET_NAME } from '../Cognee/payloads';
import { CogneeChatHistory, MAX_SESSION_PAIRS } from './memory';
import type { CogneeRequest, CogneeRequestOptions } from './memory';

type MemoryOptions = {
	datasetName?: string;
	datasetId?: string;
	windowSize?: number;
};

/**
 * AI Agent memory sub-node: persists the conversation in a Cognee session, so
 * the agent's chat history survives restarts and is readable from any Cognee
 * client. Knowledge the agent should search, as opposed to replay, belongs on
 * the Tool port instead — the Cognee node's Recall over a dataset.
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
		subtitle: 'Session memory in Cognee',
		description:
			'Store AI Agent conversation history in a Cognee session so it survives restarts and is readable from any Cognee client',
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
				displayName: 'Options',
				name: 'options',
				placeholder: 'Add Option',
				description: 'Additional options for memory management',
				type: 'collection',
				default: {},
				options: [
					{
						displayName: 'Dataset Name',
						name: 'datasetName',
						type: 'string',
						default: DEFAULT_DATASET_NAME,
						description:
							'Dataset this session is attributed to, recorded on the session the first time it is written. Later writes do not move an existing session, and sessions are keyed per user rather than per dataset, so reusing one Session ID with different datasets mixes one history rather than splitting it.',
					},
					{
						displayName: 'Dataset ID',
						name: 'datasetId',
						type: 'string',
						default: '',
						description:
							'Attribute the session by dataset UUID instead of by name. Required for a dataset shared with you, because a name only resolves among datasets you own. Takes precedence over Dataset Name.',
					},
					{
						displayName: 'Window Size',
						name: 'windowSize',
						type: 'number',
						default: 10,
						description: 'Number of recent question/answer pairs to load into the agent context',
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
		// The Window Size field caps at MAX_SESSION_PAIRS, but an expression can
		// still produce anything, and the server returns no more than that.
		const windowSize = Math.min(
			Math.max(1, Math.floor(options.windowSize || 10)),
			MAX_SESSION_PAIRS,
		);

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

		const history = new CogneeChatHistory({ sessionId, datasetName, datasetId, request });
		const memory = new WindowedChatMemory(history, { windowSize });
		return supplyMemory(this, memory);
	}
}
