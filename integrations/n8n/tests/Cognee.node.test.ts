import { describe, expect, it } from 'vitest';
import type { IExecuteSingleFunctions, IHttpRequestOptions } from 'n8n-workflow';
import { NodeConnectionTypes } from 'n8n-workflow';
import { getOperation, getParameter, node } from './helpers';

const DELETE_SET_POST_RECEIVE = {
	type: 'set',
	properties: {
		value: '={{ { "deleted": true } }}',
	},
};

describe('Cognee node description', () => {
	const { description } = node;

	it('exposes the node identity', () => {
		expect(description.name).toBe('cognee');
		expect(description.displayName).toBe('Cognee');
		expect(description.version).toBe(1);
		expect(description.group).toEqual(['transform']);
		expect(description.usableAsTool).toBe(true);
	});

	it('wires main input and output connections', () => {
		expect(description.inputs).toEqual([NodeConnectionTypes.Main]);
		expect(description.outputs).toEqual([NodeConnectionTypes.Main]);
	});

	it('requires the cogneeApi credential', () => {
		expect(description.credentials).toEqual([{ name: 'cogneeApi', required: true }]);
	});

	it('targets the credential base URL /api and authenticates via X-Api-Key', () => {
		expect(description.requestDefaults?.baseURL).toBe('={{$credentials.baseUrl}}/api');
		expect(description.requestDefaults?.headers).toEqual({
			Accept: 'application/json',
			'X-Api-Key': '={{$credentials.apiKey}}',
		});
	});

	it('exposes exactly the expected resources', () => {
		const resource = description.properties.find((p) => p.name === 'resource')!;
		const values = (resource.options as Array<{ value: string }>).map((o) => o.value);
		expect(values).toEqual([
			'addData',
			'cognify',
			'dataset',
			'delete',
			'memory',
			'search',
			'session',
			'skill',
		]);
	});
});

describe('operation routing', () => {
	it('addData.add posts to /v1/add', () => {
		const request = getOperation('addData', 'add').routing?.request;
		expect(request?.method).toBe('POST');
		expect(request?.url).toBe('/v1/add');
	});

	it('cognify.cognify posts to /v1/cognify', () => {
		const request = getOperation('cognify', 'cognify').routing?.request;
		expect(request?.method).toBe('POST');
		expect(request?.url).toBe('/v1/cognify');
	});

	it('search.search posts to /v1/search', () => {
		const request = getOperation('search', 'search').routing?.request;
		expect(request?.method).toBe('POST');
		expect(request?.url).toBe('/v1/search');
	});

	it('delete.deleteDataset deletes the dataset and replaces the body with {deleted: true}', () => {
		const routing = getOperation('delete', 'deleteDataset').routing!;
		expect(routing.request?.method).toBe('DELETE');
		expect(routing.request?.url).toBe('=/v1/datasets/{{$parameter["datasetId"]}}');
		expect(routing.output?.postReceive).toEqual([DELETE_SET_POST_RECEIVE]);
	});

	it('delete.deleteData deletes the data item and replaces the body with {deleted: true}', () => {
		const routing = getOperation('delete', 'deleteData').routing!;
		expect(routing.request?.method).toBe('DELETE');
		expect(routing.request?.url).toBe(
			'=/v1/datasets/{{$parameter["datasetId"]}}/data/{{$parameter["dataId"]}}',
		);
		expect(routing.output?.postReceive).toEqual([DELETE_SET_POST_RECEIVE]);
	});

	it('skill.ingestSkill posts the skill markdown to /v1/skills', () => {
		const request = getOperation('skill', 'ingestSkill').routing?.request;
		expect(request?.method).toBe('POST');
		expect(request?.url).toBe('/v1/skills');
		expect(request?.body).toEqual({
			skills_text: '={{$parameter["skillsText"]}}',
			skill_name: '={{$parameter["skillName"]}}',
			dataset_name: '={{$parameter["skillDatasetName"]}}',
		});
	});

	it('skill.getSkill fetches a skill by ID', () => {
		const request = getOperation('skill', 'getSkill').routing?.request;
		expect(request?.method).toBe('GET');
		expect(request?.url).toBe('=/v1/skills/{{$parameter["skillId"]}}');
	});

	it('skill.getProposal fetches a proposal by ID', () => {
		const request = getOperation('skill', 'getProposal').routing?.request;
		expect(request?.method).toBe('GET');
		expect(request?.url).toBe('=/v1/proposals/{{$parameter["proposalId"]}}');
	});

	it('skill.proposeImprovement records a skill run without applying', () => {
		const request = getOperation('skill', 'proposeImprovement').routing?.request;
		expect(request?.method).toBe('POST');
		expect(request?.url).toBe('/v1/remember/entry');
		const body = request?.body as Record<string, Record<string, unknown>>;
		expect(body.entry.type).toBe('skill_run');
		expect(body.entry.feedback).toBe(-1);
		expect(body.skill_improvement.apply).toBe(false);
		expect(body.skill_improvement.score_threshold).toBe('={{$parameter["scoreThreshold"]}}');
	});

	it('skill.applyImprovement applies a proposal by ID', () => {
		const request = getOperation('skill', 'applyImprovement').routing?.request;
		expect(request?.method).toBe('POST');
		expect(request?.url).toBe('/v1/remember/entry');
		const body = request?.body as Record<string, Record<string, unknown>>;
		expect(body.skill_improvement.apply).toBe(true);
		expect(body.skill_improvement.proposal_id).toBe('={{$parameter["proposalId"]}}');
	});

	it('skill.reviewSkill runs an AGENTIC_COMPLETION search with a postReceive transform', () => {
		const routing = getOperation('skill', 'reviewSkill').routing!;
		expect(routing.request?.method).toBe('POST');
		expect(routing.request?.url).toBe('/v1/search');
		const body = routing.request?.body as Record<string, unknown>;
		expect(body.search_type).toBe('AGENTIC_COMPLETION');
		expect(body.query).toBe('={{$parameter["reviewQuery"]}}');
		expect(body.datasets).toBe('={{[$parameter["skillDatasetName"]]}}');
		expect(body.skills).toBe('={{[$parameter["skillName"]]}}');
		expect(typeof routing.output?.postReceive?.[0]).toBe('function');
	});
});

describe('parameter routing', () => {
	it('routes addData fields through the registered multipart hook', async () => {
		const context = {
			getNodeParameter: (name: string, fallback?: unknown) =>
				(({ datasetName: 'docs', textData: ['first', 'second'] }) as Record<string, unknown>)[
					name
				] ?? fallback,
		} as IExecuteSingleFunctions;
		const hooks = getOperation('addData', 'add').routing?.send?.preSend;
		expect(hooks).toHaveLength(1);
		const request = await hooks![0].call(context, { url: '/v1/add' } as IHttpRequestOptions);
		const form = await new Response(new Uint8Array(request.body as Buffer), {
			headers: { 'Content-Type': String(request.headers?.['Content-Type']) },
		}).formData();
		expect(form.get('datasetName')).toBe('docs');
		const files = form.getAll('data') as File[];
		expect(files.map((file) => file.name)).toEqual(['text-1.txt', 'text-2.txt']);
		expect(await Promise.all(files.map((file) => file.text()))).toEqual(['first', 'second']);
	});

	it('offers exactly the supported search types', () => {
		const searchType = getParameter('searchType', 'search');
		const values = (searchType.options as Array<{ value: string }>).map((o) => o.value);
		expect(values).toEqual([
			'AGENTIC_COMPLETION',
			'GRAPH_COMPLETION_COT',
			'CHUNKS',
			'CHUNKS_LEXICAL',
			'CODE',
			'CODING_RULES',
			'CYPHER',
			'FEELING_LUCKY',
			'GRAPH_COMPLETION',
			'GRAPH_COMPLETION_CONTEXT_EXTENSION',
			'GRAPH_COMPLETION_DECOMPOSITION',
			'GRAPH_REPORT',
			'GRAPH_SUMMARY_COMPLETION',
			'HYBRID_COMPLETION',
			'NATURAL_LANGUAGE',
			'RAG_COMPLETION',
			'SUMMARIES',
			'TEMPORAL',
			'TRIPLET_COMPLETION',
		]);
		expect(searchType.routing?.request?.body).toEqual({ search_type: '={{$value}}' });
	});

	it('routes search fields into the request body', () => {
		expect(getParameter('query', 'search').routing?.request?.body).toEqual({
			query: '={{$value}}',
		});
		expect(getParameter('datasets', 'search').routing?.request?.body).toEqual({
			datasets: '={{$value}}',
		});
	});

	it('routes the skill dataset ID as a query-string parameter', () => {
		expect(getParameter('getDatasetId', 'skill').routing?.request?.qs).toEqual({
			dataset_id: '={{$value}}',
		});
	});
});
