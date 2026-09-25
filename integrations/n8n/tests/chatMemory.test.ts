import { describe, expect, it } from 'vitest';
import type { ISupplyDataFunctions } from 'n8n-workflow';

import {
	CogneeChatHistory,
	ROLE_MARKER,
	assertEntryStored,
	sessionEntriesToMessages,
} from '../nodes/CogneeMemory/memory';
import type { CogneeRequestOptions } from '../nodes/CogneeMemory/memory';
import { CogneeMemory } from '../nodes/CogneeMemory/CogneeMemory.node';

type Call = CogneeRequestOptions;

function fakeRequest(responses: Record<string, unknown | (() => unknown)> = {}) {
	const calls: Call[] = [];
	const request = async (options: Call) => {
		calls.push(options);
		const key = `${options.method} ${options.url}`;
		// Writes answer like the real endpoint, so assertEntryStored is satisfied.
		if (!(key in responses)) {
			return options.method === 'POST' ? { status: 'session_stored', entry_id: 'qa-1' } : {};
		}
		const value = responses[key];
		return typeof value === 'function' ? (value as () => unknown)() : value;
	};
	return { calls, request };
}

const sessionBody = {
	session_id: 'chat-1',
	qas: [
		{ time: '2026-01-01T00:00:00Z', question: 'Hi', answer: 'Hello!', qa_id: 'q1' },
		{
			time: '2026-01-01T00:01:00Z',
			question: 'Where was Einstein born?',
			answer: 'Ulm.',
			qa_id: 'q2',
		},
		{ time: '2026-01-01T00:02:00Z', question: '', answer: '' },
	],
	traces: [],
};

describe('sessionEntriesToMessages', () => {
	it('maps Q&A entries to alternating user/assistant messages, oldest first, skipping empty rows', () => {
		expect(sessionEntriesToMessages(sessionBody)).toEqual([
			{ role: 'user', content: [{ type: 'text', text: 'Hi' }] },
			{ role: 'assistant', content: [{ type: 'text', text: 'Hello!' }] },
			{ role: 'user', content: [{ type: 'text', text: 'Where was Einstein born?' }] },
			{ role: 'assistant', content: [{ type: 'text', text: 'Ulm.' }] },
		]);
	});

	it('emits only the sides that were written, and honours the role marker', () => {
		expect(
			sessionEntriesToMessages({
				qas: [
					{ question: 'Q', answer: '' },
					{ question: '', answer: 'A' },
					{ question: '', answer: 'be terse', context: `${ROLE_MARKER}system` },
					{ question: '', answer: 'x', context: `${ROLE_MARKER}bogus` },
				],
			}),
		).toEqual([
			{ role: 'user', content: [{ type: 'text', text: 'Q' }] },
			{ role: 'assistant', content: [{ type: 'text', text: 'A' }] },
			{ role: 'system', content: [{ type: 'text', text: 'be terse' }] },
		]);
	});

	it('tolerates unexpected shapes', () => {
		expect(sessionEntriesToMessages(null)).toEqual([]);
		expect(sessionEntriesToMessages({ qas: 'nope' })).toEqual([]);
		expect(sessionEntriesToMessages({ qas: [null, 5] })).toEqual([]);
	});
});

describe('CogneeChatHistory reads', () => {
	it('loads the session from GET /v1/sessions/{id}, url-encoding the id', async () => {
		const { calls, request } = fakeRequest({ 'GET /v1/sessions/chat%201': sessionBody });
		const messages = await new CogneeChatHistory({
			sessionId: 'chat 1',
			datasetName: 'main_dataset',
			request,
		}).getMessages();
		expect(calls).toEqual([{ method: 'GET', url: '/v1/sessions/chat%201', allowNotFound: true }]);
		expect(messages.map((m) => m.role)).toEqual(['user', 'assistant', 'user', 'assistant']);
		expect(messages[3].content).toEqual([{ type: 'text', text: 'Ulm.' }]);
	});

	// The transport turns an allowed 404 into undefined; the node suite covers the
	// end-to-end version where a real 404 comes back from n8n's HTTP helper.
	it('reads an absent session as an empty history and propagates other failures', async () => {
		const notFound = fakeRequest({ 'GET /v1/sessions/new': () => undefined });
		expect(
			await new CogneeChatHistory({
				sessionId: 'new',
				datasetName: 'd',
				request: notFound.request,
			}).getMessages(),
		).toEqual([]);

		const failing = fakeRequest({
			'GET /v1/sessions/new': () => {
				throw new Error('unauthorized');
			},
		});
		await expect(
			new CogneeChatHistory({
				sessionId: 'new',
				datasetName: 'd',
				request: failing.request,
			}).getMessages(),
		).rejects.toThrow(/unauthorized/);
	});
});

describe('CogneeChatHistory (Chat Memory Manager path)', () => {
	const entriesOf = (calls: Call[]) =>
		calls.map((c) => (c.body as { entry: Record<string, unknown> }).entry);

	it('stores a full agent turn as one paired entry', async () => {
		const { calls, request } = fakeRequest();
		const history = new CogneeChatHistory({
			sessionId: 'chat-1',
			datasetName: 'main_dataset',
			request,
		});
		await history.addMessages([
			{ role: 'user', content: [{ type: 'text', text: 'Q1' }] },
			{ role: 'assistant', content: [{ type: 'text', text: 'A1' }] },
		]);
		expect(entriesOf(calls)).toEqual([{ type: 'qa', question: 'Q1', answer: 'A1', context: '' }]);
	});

	// Regression: the Chat Memory Manager node adds messages one at a time and
	// never calls addMessages, so anything buffered waiting for a counterpart
	// was silently dropped when the instance went away.
	it('writes a lone user message immediately, with the answer side left empty', async () => {
		const { calls, request } = fakeRequest();
		const history = new CogneeChatHistory({
			sessionId: 'chat-1',
			datasetName: 'main_dataset',
			request,
		});
		await history.addMessage({ role: 'user', content: [{ type: 'text', text: 'remember this' }] });
		expect(entriesOf(calls)).toEqual([
			{ type: 'qa', question: 'remember this', answer: '', context: '' },
		]);
	});

	it('keeps each one-at-a-time message on its own side, and system roles in context', async () => {
		const { calls, request } = fakeRequest();
		const history = new CogneeChatHistory({
			sessionId: 'chat-1',
			datasetName: 'main_dataset',
			request,
		});
		for (const message of [
			{ role: 'user' as const, content: [{ type: 'text' as const, text: 'Q' }] },
			{ role: 'assistant' as const, content: [{ type: 'text' as const, text: 'A' }] },
			{ role: 'system' as const, content: [{ type: 'text' as const, text: 'be terse' }] },
		]) {
			await history.addMessage(message);
		}
		expect(entriesOf(calls)).toEqual([
			{ type: 'qa', question: 'Q', answer: '', context: '' },
			{ type: 'qa', question: '', answer: 'A', context: '' },
			{ type: 'qa', question: '', answer: 'be terse', context: `${ROLE_MARKER}system` },
		]);
	});

	// The review case: inserting Q then A one at a time used to round-trip as four
	// messages, two of them invented placeholder text.
	it('round-trips one-at-a-time inserts back to exactly the messages inserted', async () => {
		const { calls, request } = fakeRequest();
		const history = new CogneeChatHistory({
			sessionId: 'chat-1',
			datasetName: 'main_dataset',
			request,
		});
		const inserted = [
			{ role: 'system' as const, content: [{ type: 'text' as const, text: 'be terse' }] },
			{ role: 'user' as const, content: [{ type: 'text' as const, text: 'Q' }] },
			{ role: 'assistant' as const, content: [{ type: 'text' as const, text: 'A' }] },
		];
		for (const message of inserted) await history.addMessage(message);

		// Replay what the server would return for those writes.
		const qas = entriesOf(calls).map((entry) => ({
			question: entry.question,
			answer: entry.answer,
			context: entry.context,
		}));
		expect(sessionEntriesToMessages({ qas })).toEqual(inserted);
	});

	it('skips a message with no text rather than storing an empty entry', async () => {
		const { calls, request } = fakeRequest();
		const history = new CogneeChatHistory({
			sessionId: 'chat-1',
			datasetName: 'main_dataset',
			request,
		});
		await history.addMessage({ role: 'user', content: [] });
		expect(calls).toHaveLength(0);
	});

	it('fails loudly when Cognee answers 200 but did not store the turn', async () => {
		const { request } = fakeRequest({
			'POST /v1/remember/entry': () => ({ status: 'errored', error: 'add_qa returned None' }),
		});
		const history = new CogneeChatHistory({
			sessionId: 'chat-1',
			datasetName: 'main_dataset',
			request,
		});
		await expect(
			history.addMessage({ role: 'user', content: [{ type: 'text', text: 'Q' }] }),
		).rejects.toThrow(/did not store the conversation turn/);
	});

	it('sends a dataset ID when given one, for a shared dataset', async () => {
		const { calls, request } = fakeRequest();
		const history = new CogneeChatHistory({
			sessionId: 'chat-1',
			datasetName: 'main_dataset',
			datasetId: 'd-uuid',
			request,
		});
		await history.addMessage({ role: 'user', content: [{ type: 'text', text: 'Q' }] });
		expect(calls[0].body).toMatchObject({ dataset_id: 'd-uuid' });
	});

	it('rejects clear() naming the Chat Memory Manager operations it blocks', async () => {
		const { calls, request } = fakeRequest();
		const history = new CogneeChatHistory({
			sessionId: 'chat-1',
			datasetName: 'main_dataset',
			request,
		});
		await expect(history.clear()).rejects.toThrow(/Delete Messages.*Override All Messages/s);
		expect(calls).toHaveLength(0);
	});
});

describe('assertEntryStored', () => {
	it('accepts a stored entry and rejects an errored or id-less one', () => {
		expect(() => assertEntryStored({ status: 'session_stored', entry_id: 'q1' })).not.toThrow();
		expect(() => assertEntryStored(undefined)).not.toThrow();
		expect(() => assertEntryStored({ status: 'errored' })).toThrow(/did not store/);
		expect(() => assertEntryStored({ status: 'session_stored' })).toThrow(/did not store/);
		expect(() => assertEntryStored({ error: 'boom' })).toThrow(/boom/);
	});
});

describe('CogneeMemory node', () => {
	it('declares itself as an AI memory sub-node', () => {
		const node = new CogneeMemory();
		expect(node.description.outputs).toEqual(['ai_memory']);
		expect(node.description.inputs).toEqual([]);
		expect(node.description.credentials).toEqual([{ name: 'cogneeApi', required: true }]);
		expect(node.description.codex?.subcategories?.AI).toContain('Memory');
	});

	it('supplies a memory that calls the Cognee API through the credential', async () => {
		const http: Array<Record<string, unknown>> = [];
		const params: Record<string, unknown> = {
			sessionId: 'chat-7',
			options: { datasetName: 'support', windowSize: 2 },
		};
		const ctx = {
			getNodeParameter: (name: string, _i: number, fallback?: unknown) =>
				name in params ? params[name] : fallback,
			getNode: () => ({
				name: 'Cognee Memory',
				type: 'cogneeMemory',
				typeVersion: 1,
				position: [0, 0],
				parameters: {},
			}),
			getCredentials: async () => ({ baseUrl: 'https://tenant.example.cognee.ai/', apiKey: 'k' }),
			addInputData: () => ({ index: 0 }),
			addOutputData: () => undefined,
			helpers: {
				httpRequestWithAuthentication: async (_type: string, options: Record<string, unknown>) => {
					http.push(options);
					if (options.method === 'GET') return sessionBody;
					return { entry_type: 'qa', entry_id: 'q9' };
				},
			},
		} as unknown as ISupplyDataFunctions;

		const supplied = await new CogneeMemory().supplyData.call(ctx, 0);
		const lcMemory = supplied.response as {
			loadMemoryVariables: (v: Record<string, unknown>) => Promise<{ chat_history: unknown[] }>;
			saveContext: (i: Record<string, unknown>, o: Record<string, unknown>) => Promise<void>;
		};

		const { chat_history } = await lcMemory.loadMemoryVariables({ input: 'x' });
		expect(chat_history).toHaveLength(4); // window of 2 pairs
		expect(http[0]).toMatchObject({
			method: 'GET',
			url: 'https://tenant.example.cognee.ai/api/v1/sessions/chat-7',
			json: true,
		});

		await lcMemory.saveContext({ input: 'New question' }, { output: 'New answer' });
		expect(http[1]).toMatchObject({
			method: 'POST',
			url: 'https://tenant.example.cognee.ai/api/v1/remember/entry',
			body: {
				entry: { type: 'qa', question: 'New question', answer: 'New answer', context: '' },
				session_id: 'chat-7',
				dataset_name: 'support',
			},
		});
	});

	it('turns a 404 on the session lookup into an empty history and wraps other failures as NodeApiError', async () => {
		const make = (fail: () => never) =>
			({
				getNodeParameter: (name: string, _i: number, fallback?: unknown) =>
					name === 'sessionId' ? 'fresh' : fallback,
				getNode: () => ({
					name: 'Cognee Memory',
					type: 'cogneeMemory',
					typeVersion: 1,
					position: [0, 0],
					parameters: {},
				}),
				getCredentials: async () => ({ baseUrl: 'https://c.example', apiKey: 'k' }),
				addInputData: () => ({ index: 0 }),
				addOutputData: () => undefined,
				helpers: { httpRequestWithAuthentication: async () => fail() },
			}) as unknown as ISupplyDataFunctions;

		const missing = await new CogneeMemory().supplyData.call(
			make(() => {
				throw Object.assign(new Error('not found'), { httpCode: '404' });
			}),
			0,
		);
		const asLc = (m: unknown) =>
			m as {
				loadMemoryVariables: (v: Record<string, unknown>) => Promise<{ chat_history: unknown[] }>;
			};
		expect((await asLc(missing.response).loadMemoryVariables({})).chat_history).toEqual([]);

		const broken = await new CogneeMemory().supplyData.call(
			make(() => {
				throw Object.assign(new Error('forbidden'), { httpCode: '403' });
			}),
			0,
		);
		await expect(asLc(broken.response).loadMemoryVariables({})).rejects.toThrow(/Forbidden/);
	});

	it('rejects an empty session id', async () => {
		const ctx = {
			getNodeParameter: (_n: string, _i: number, fallback?: unknown) => fallback,
			getNode: () => ({
				name: 'Cognee Memory',
				type: 'cogneeMemory',
				typeVersion: 1,
				position: [0, 0],
				parameters: {},
			}),
		} as unknown as ISupplyDataFunctions;
		await expect(new CogneeMemory().supplyData.call(ctx, 0)).rejects.toThrow(
			/Session ID is required/,
		);
	});
});
