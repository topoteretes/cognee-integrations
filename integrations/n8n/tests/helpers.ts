import type {
	IExecuteSingleFunctions,
	IN8nHttpFullResponse,
	INodeExecutionData,
	INodeProperties,
	INodePropertyOptions,
} from 'n8n-workflow';
import { Cognee } from '../nodes/Cognee/Cognee.node';

export const node = new Cognee();

// Find the 'operation' options property scoped to the given resource.
export function getOperationProperty(resource: string): INodeProperties {
	const prop = node.description.properties.find(
		(p) =>
			p.name === 'operation' &&
			((p.displayOptions?.show?.resource as string[] | undefined) ?? []).includes(resource),
	);
	if (!prop) {
		throw new Error(`No operation property found for resource "${resource}"`);
	}
	return prop;
}

// Find a specific operation option under a resource.
export function getOperation(resource: string, operation: string): INodePropertyOptions {
	const options = getOperationProperty(resource).options as INodePropertyOptions[];
	const opt = options.find((o) => o.value === operation);
	if (!opt) {
		throw new Error(`No operation "${operation}" found on resource "${resource}"`);
	}
	return opt;
}

// Find a top-level parameter property scoped to the given resource.
export function getParameter(name: string, resource: string): INodeProperties {
	const prop = node.description.properties.find(
		(p) =>
			p.name === name &&
			((p.displayOptions?.show?.resource as string[] | undefined) ?? []).includes(resource),
	);
	if (!prop) {
		throw new Error(`No parameter "${name}" found for resource "${resource}"`);
	}
	return prop;
}

type PostReceiveFn = (
	this: IExecuteSingleFunctions,
	items: INodeExecutionData[],
	response: IN8nHttpFullResponse,
) => Promise<INodeExecutionData[]>;

// Retrieve the reviewSkill postReceive transform from the node description.
export function getReviewPostReceive(): PostReceiveFn {
	const actions = getOperation('skill', 'reviewSkill').routing?.output?.postReceive ?? [];
	const fn = actions.find((a) => typeof a === 'function');
	if (!fn) {
		throw new Error('reviewSkill has no postReceive function');
	}
	return fn as PostReceiveFn;
}

// Build a mocked Cognee API HTTP response.
export function mockResponse(body: unknown): IN8nHttpFullResponse {
	return { body, headers: {}, statusCode: 200 } as IN8nHttpFullResponse;
}

// Run the reviewSkill postReceive hook against a mocked /v1/search response body.
export async function runReview(body: unknown): Promise<Record<string, unknown>> {
	const fn = getReviewPostReceive();
	const out = await fn.call({} as IExecuteSingleFunctions, [{ json: {} }], mockResponse(body));
	return out[0].json as Record<string, unknown>;
}
