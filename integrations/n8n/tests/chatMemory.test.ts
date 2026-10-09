import { describe, expect, it } from 'vitest';
import type { ISupplyDataFunctions } from 'n8n-workflow';

import {
	CogneeAgentMemory,
	CogneeChatHistory,
	MAX_PENDING_PER_TARGET,
	MAX_PENDING_SESSIONS,
	ROLE_MARKER,
	assertEntryStored,
	markSessionPending,
	normalizeState,
	pendingBatches,
	promotionDue,
	sessionEntriesToMessages,
} from '../nodes/CogneeMemory/memory';
import type { CogneeRequestOptions, PromotionState } from '../nodes/CogneeMemory/memory';
import {
	CogneeMemory,
	STATIC_DATA_KEY,
	finiteNumber,
	resolveRecallSettings,
} from '../nodes/CogneeMemory/CogneeMemory.node';

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

/** A recall hit in the cognee >= 1.6 only_context shape. */
function onlyContextHit(query: string, context: string, guidance = ''): Record<string, unknown> {
	const tail = guidance ? `\`\n\n## Active session guidance\n${guidance}` : '`';
	return {
		kind: 'graph_completion',
		search_type: 'HYBRID_COMPLETION',
		text: `The question is: \`${query}\`\nAnswer using this sectioned context.\n\nContext:\n\`${context}${tail}`,
	};
}

const HOUR = 3_600_000;

function agentMemory(
	overrides: Partial<ConstructorParameters<typeof CogneeAgentMemory>[0]> = {},
	responses: Record<string, unknown | (() => unknown)> = {},
) {
	const { calls, request } = fakeRequest({
		'GET /v1/sessions/chat-1': sessionBody,
		...responses,
	});
	const warnings: string[] = [];
	const state: PromotionState = {};
	let clock = Date.parse('2026-10-09T08:00:00Z');
	const memory = new CogneeAgentMemory({
		chatHistory: new CogneeChatHistory({ sessionId: 'chat-1', datasetName: 'support', request }),
		sessionId: 'chat-1',
		windowSize: 5,
		request,
		recall: {
			query: 'what sport do I play?',
			searchType: 'HYBRID_COMPLETION',
			scope: ['graph'],
			datasets: ['support'],
			topK: 5,
			maxChars: 12000,
			role: 'system',
		},
		promotion: {
			policy: { mode: 'interval', intervalMs: 24 * HOUR },
			target: { datasetName: 'support' },
			state,
		},
		warn: (message) => warnings.push(message),
		now: () => clock,
		...overrides,
	});
	return { memory, calls, warnings, state, tick: (ms: number) => (clock += ms) };
}

describe('CogneeAgentMemory load', () => {
	it('returns the session window followed by one recalled-context message', async () => {
		const { memory, calls } = agentMemory(
			{},
			{ 'POST /v1/recall': [onlyContextHit('what sport do I play?', 'Andrej plays tennis.')] },
		);
		const messages = await memory.loadMessages();
		expect(messages.map((m) => m.role)).toEqual([
			'user',
			'assistant',
			'user',
			'assistant',
			'system',
		]);
		const last = messages[messages.length - 1];
		expect(last.content[0]).toMatchObject({ type: 'text' });
		expect((last.content[0] as { text: string }).text).toContain('Andrej plays tennis.');
		expect((last.content[0] as { text: string }).text).not.toContain('The question is:');

		const recall = calls.find((c) => c.url === '/v1/recall');
		expect(recall?.body).toEqual({
			query: 'what sport do I play?',
			search_type: 'HYBRID_COMPLETION',
			datasets: ['support'],
			top_k: 5,
			session_id: 'chat-1',
			scope: 'graph',
			only_context: true,
			context_format: 'context',
		});
	});

	it('recalls once per instance even when the agent loads several times', async () => {
		const { memory, calls } = agentMemory(
			{},
			{ 'POST /v1/recall': [onlyContextHit('what sport do I play?', 'tennis')] },
		);
		await memory.loadMessages();
		await memory.loadMessages();
		expect(calls.filter((c) => c.url === '/v1/recall')).toHaveLength(1);
		expect(calls.filter((c) => c.method === 'GET')).toHaveLength(2);
	});

	it('injects nothing when recall returns no context, is off, or has no query', async () => {
		const empty = agentMemory({}, { 'POST /v1/recall': [] });
		expect((await empty.memory.loadMessages()).map((m) => m.role)).not.toContain('system');

		const off = agentMemory({ recall: undefined });
		expect(await off.memory.loadMessages()).toHaveLength(4);
		expect(off.calls.filter((c) => c.url === '/v1/recall')).toHaveLength(0);

		const blank = agentMemory({
			recall: {
				query: '  ',
				searchType: 'HYBRID_COMPLETION',
				scope: ['graph'],
				datasets: [],
				topK: 5,
				maxChars: 1000,
				role: 'system',
			},
		});
		await blank.memory.loadMessages();
		expect(blank.calls.filter((c) => c.url === '/v1/recall')).toHaveLength(0);
	});

	it('fails open when recall errors: history is returned and a warning is logged', async () => {
		const { memory, warnings } = agentMemory(
			{},
			{
				'POST /v1/recall': () => {
					throw new Error('recall exploded');
				},
			},
		);
		expect(await memory.loadMessages()).toHaveLength(4);
		expect(warnings[0]).toMatch(/recall failed.*recall exploded/);
	});

	it('uses the user role when asked', async () => {
		const { memory } = agentMemory(
			{
				recall: {
					query: 'q',
					searchType: 'HYBRID_COMPLETION',
					scope: ['graph'],
					datasets: ['support'],
					topK: 5,
					maxChars: 1000,
					role: 'user',
				},
			},
			{ 'POST /v1/recall': ['bare context from an older server'] },
		);
		const messages = await memory.loadMessages();
		expect(messages[messages.length - 1].role).toBe('user');
		expect((messages[messages.length - 1].content[0] as { text: string }).text).toContain(
			'bare context from an older server',
		);
	});
});

describe('CogneeAgentMemory save and promotion', () => {
	it('stores the turn and marks the session pending under its dataset', async () => {
		const { memory, calls, state } = agentMemory();
		await memory.saveTurn('I play tennis', 'Noted!');
		expect(calls[0]).toMatchObject({
			method: 'POST',
			url: '/v1/remember/entry',
			body: { entry: { question: 'I play tennis', answer: 'Noted!' }, session_id: 'chat-1' },
		});
		expect(pendingBatches(state)).toEqual([{ key: 'name:support', sessionIds: ['chat-1'] }]);
	});

	it('promotes on the first execution, then not again until the interval has passed', async () => {
		const { memory, calls, state, tick } = agentMemory();
		await memory.saveTurn('q', 'a');

		expect(await memory.promoteIfDue()).toEqual({
			outcome: 'ran',
			batches: [
				{ target: { datasetName: 'support' }, sessionIds: ['chat-1'], status: 'submitted' },
			],
		});
		const improve = calls.find((c) => c.url === '/v1/improve');
		expect(improve?.body).toEqual({
			session_ids: ['chat-1'],
			dataset_name: 'support',
			run_in_background: true,
		});
		expect(state.lastImproveAt).toBe('2026-10-09T08:00:00.000Z');
		expect(pendingBatches(state)).toEqual([]);

		tick(2 * HOUR);
		await memory.saveTurn('q2', 'a2');
		expect(await memory.promoteIfDue()).toEqual({ outcome: 'skipped', reason: 'not_due' });
		expect(calls.filter((c) => c.url === '/v1/improve')).toHaveLength(1);

		tick(23 * HOUR);
		expect(await memory.promoteIfDue()).toMatchObject({ outcome: 'ran' });
		expect(calls.filter((c) => c.url === '/v1/improve')).toHaveLength(2);
	});

	it('sends one request per dataset, each covering the sessions written into it', async () => {
		const { memory, calls, state } = agentMemory();
		state.lastImproveAt = '2026-10-01T00:00:00Z';
		state.pending = {
			'name:support': { 'chat-7': '2026-10-08T00:00:00Z' },
			'id:ds-uuid': { 'chat-9': '2026-10-08T01:00:00Z' },
		};
		await memory.saveTurn('q', 'a');
		const result = await memory.promoteIfDue();
		expect(result.outcome).toBe('ran');
		const improves = calls.filter((c) => c.url === '/v1/improve').map((c) => c.body);
		expect(improves).toEqual(
			expect.arrayContaining([
				expect.objectContaining({ session_ids: ['chat-7', 'chat-1'], dataset_name: 'support' }),
				expect.objectContaining({ session_ids: ['chat-9'], dataset_id: 'ds-uuid' }),
			]),
		);
		expect(pendingBatches(state)).toEqual([]);
	});

	it('acknowledges only the submitted snapshot', async () => {
		const { memory, state } = agentMemory();
		state.pending = { 'name:support': { 'chat-7': '2026-10-08T00:00:00Z' } };
		// A write that lands while the request is in flight stays pending.
		const { calls, request } = fakeRequest({
			'POST /v1/improve': () => {
				state.pending!['name:support']['chat-late'] = '2026-10-09T08:00:00Z';
				return { status: 'running' };
			},
		});
		const memory2 = new CogneeAgentMemory({
			chatHistory: new CogneeChatHistory({ sessionId: 'chat-1', datasetName: 'support', request }),
			sessionId: 'chat-1',
			windowSize: 5,
			request,
			promotion: {
				policy: { mode: 'eachExecution', intervalMs: HOUR },
				target: { datasetName: 'support' },
				state,
			},
		});
		void memory;
		await memory2.promoteIfDue();
		expect(calls[0].body).toMatchObject({ session_ids: ['chat-7'] });
		expect(pendingBatches(state)).toEqual([{ key: 'name:support', sessionIds: ['chat-late'] }]);
	});

	it('keeps sessions queued on a busy server unless the holder was asked to rerun', async () => {
		const busy = agentMemory({}, { 'POST /v1/improve': {} });
		await busy.memory.saveTurn('q', 'a');
		expect(await busy.memory.promoteIfDue()).toMatchObject({
			outcome: 'ran',
			batches: [{ status: 'busy', sessionIds: ['chat-1'] }],
		});
		expect(busy.state.lastImproveAt).toBeUndefined();
		expect(pendingBatches(busy.state)).toEqual([{ key: 'name:support', sessionIds: ['chat-1'] }]);
		// Next execution retries straight away.
		expect(await busy.memory.promoteIfDue()).toMatchObject({ outcome: 'ran' });

		const covered = agentMemory(
			{},
			{
				'POST /v1/improve': {
					status: 'skipped',
					rerun_requested: true,
					stages: [{ stage: 'persist_session_qa', status: 'skipped', reason: 'lock_held' }],
				},
			},
		);
		await covered.memory.saveTurn('q', 'a');
		expect(await covered.memory.promoteIfDue()).toMatchObject({
			batches: [{ status: 'submitted' }],
		});
		expect(covered.state.lastImproveAt).toBeDefined();
		expect(pendingBatches(covered.state)).toEqual([]);
	});

	it('keeps sessions queued and warns when the request fails', async () => {
		const failing = agentMemory(
			{},
			{
				'POST /v1/improve': () => {
					throw new Error('HTTP 503');
				},
			},
		);
		await failing.memory.saveTurn('q', 'a');
		expect(await failing.memory.promoteIfDue()).toMatchObject({
			batches: [{ status: 'failed', error: 'HTTP 503', sessionIds: ['chat-1'] }],
		});
		expect(failing.state.lastImproveAt).toBeUndefined();
		expect(pendingBatches(failing.state)).toEqual([
			{ key: 'name:support', sessionIds: ['chat-1'] },
		]);
		expect(failing.warnings[0]).toMatch(
			/could not promote 1 session\(s\) into "support".*HTTP 503/,
		);
	});

	it('advances the timestamp only when every dataset was accepted', async () => {
		let n = 0;
		const { memory, state } = agentMemory(
			{},
			{
				'POST /v1/improve': () => {
					n += 1;
					if (n === 1) throw new Error('boom');
					return { status: 'running' };
				},
			},
		);
		state.pending = {
			'name:a': { s1: '2026-10-08T00:00:00Z' },
			'name:b': { s2: '2026-10-08T00:00:00Z' },
		};
		const result = await memory.promoteIfDue();
		expect(result.outcome === 'ran' && result.batches.map((b) => b.status).sort()).toEqual([
			'failed',
			'submitted',
		]);
		expect(state.lastImproveAt).toBeUndefined();
		expect(pendingBatches(state)).toHaveLength(1);
	});

	it('honours the never and eachExecution modes', async () => {
		const never = agentMemory({
			promotion: {
				policy: { mode: 'never', intervalMs: HOUR },
				target: { datasetName: 'support' },
				state: {},
			},
		});
		await never.memory.saveTurn('q', 'a');
		expect(await never.memory.promoteIfDue()).toEqual({ outcome: 'skipped', reason: 'disabled' });
		expect(never.calls.filter((c) => c.url === '/v1/improve')).toHaveLength(0);

		const each = agentMemory({
			promotion: {
				policy: { mode: 'eachExecution', intervalMs: HOUR },
				target: { datasetName: 'support', datasetId: 'ds-uuid' },
				state: {},
			},
		});
		await each.memory.saveTurn('q', 'a');
		await each.memory.promoteIfDue();
		await each.memory.saveTurn('q', 'a');
		await each.memory.promoteIfDue();
		const improves = each.calls.filter((c) => c.url === '/v1/improve');
		expect(improves).toHaveLength(2);
		expect(improves[0].body).toMatchObject({ dataset_id: 'ds-uuid' });
	});

	it('skips when nothing is pending', async () => {
		const { memory, calls } = agentMemory();
		expect(await memory.promoteIfDue()).toEqual({ outcome: 'skipped', reason: 'nothing_pending' });
		expect(calls.filter((c) => c.url === '/v1/improve')).toHaveLength(0);
	});
});

describe('promotion state helpers', () => {
	const policy = { mode: 'interval' as const, intervalMs: HOUR };

	it('is due when nothing was ever promoted, or the record is unreadable', () => {
		expect(promotionDue({}, policy, 0)).toBe(true);
		expect(promotionDue({ lastImproveAt: 'garbage' }, policy, 0)).toBe(true);
		expect(
			promotionDue(
				{ lastImproveAt: '2026-10-09T08:00:00Z' },
				policy,
				Date.parse('2026-10-09T08:30:00Z'),
			),
		).toBe(false);
		expect(
			promotionDue(
				{ lastImproveAt: '2026-10-09T08:00:00Z' },
				policy,
				Date.parse('2026-10-09T09:00:00Z'),
			),
		).toBe(true);
	});

	it('is due early once the queue reaches the cap, so nothing has to be dropped', () => {
		const state: PromotionState = { lastImproveAt: '2026-10-09T08:00:00Z' };
		for (let i = 0; i < MAX_PENDING_SESSIONS - 1; i++) {
			markSessionPending(state, { datasetName: i % 2 ? 'a' : 'b' }, `s-${i}`, 0);
		}
		expect(promotionDue(state, policy, Date.parse('2026-10-09T08:01:00Z'))).toBe(false);
		markSessionPending(state, { datasetName: 'a' }, 'one-more', 0);
		expect(promotionDue(state, policy, Date.parse('2026-10-09T08:01:00Z'))).toBe(true);
	});

	it('evicts the oldest sessions of one target only past the hard cap, and reports them', () => {
		const state: PromotionState = {};
		const target = { datasetName: 'a' };
		for (let i = 0; i < MAX_PENDING_PER_TARGET; i++) {
			expect(markSessionPending(state, target, `s-${i}`, i * 1000)).toEqual([]);
		}
		expect(markSessionPending(state, target, 'overflow', MAX_PENDING_PER_TARGET * 1000)).toEqual([
			's-0',
		]);
		expect(Object.keys(state.pending!['name:a'])).toHaveLength(MAX_PENDING_PER_TARGET);
	});

	it('migrates the 0.8.0 single-bucket record into the current target', () => {
		const state: PromotionState = {
			lastImproveAt: '2026-10-09T08:00:00Z',
			pendingSessions: { old: '2026-10-09T07:00:00Z' },
		};
		normalizeState(state, { datasetName: 'support', datasetId: 'uuid' });
		expect(state).toEqual({
			lastImproveAt: '2026-10-09T08:00:00Z',
			pending: { 'id:uuid': { old: '2026-10-09T07:00:00Z' } },
		});
	});
});

describe('finiteNumber', () => {
	it('clamps, floors and falls back for anything an expression can produce', () => {
		const bounds = { default: 5, min: 1, max: 20, integer: true };
		expect(finiteNumber(undefined, bounds)).toBe(5);
		expect(finiteNumber('12', bounds)).toBe(12);
		expect(finiteNumber(' 7.9 ', bounds)).toBe(7);
		// Infinity is never a deliberate setting: it is a broken expression, so
		// it falls back to the default instead of pinning the option at a bound.
		expect(finiteNumber(Infinity, bounds)).toBe(5);
		expect(finiteNumber(-Infinity, bounds)).toBe(5);
		expect(finiteNumber(999, bounds)).toBe(20);
		expect(finiteNumber(NaN, bounds)).toBe(5);
		expect(finiteNumber('abc', bounds)).toBe(5);
		expect(finiteNumber(0, bounds)).toBe(1);
		expect(finiteNumber(0.25, { default: 24, min: 0.01 })).toBe(0.25);
		expect(finiteNumber(Infinity, { default: 12000, min: 200, integer: true })).toBe(12000);
	});
});

describe('resolveRecallSettings', () => {
	it('defaults to the memory dataset, graph scope and the system role', () => {
		expect(resolveRecallSettings({}, 'hi', { datasetName: 'support' })).toEqual({
			query: 'hi',
			searchType: 'HYBRID_COMPLETION',
			scope: ['graph'],
			datasets: ['support'],
			datasetIds: undefined,
			topK: 5,
			maxChars: 12000,
			role: 'system',
		});
	});

	it('prefers the dataset ID, then explicit recall datasets', () => {
		expect(
			resolveRecallSettings({}, 'hi', { datasetName: 'support', datasetId: 'uuid' }),
		).toMatchObject({ datasets: [], datasetIds: ['uuid'] });
		expect(
			resolveRecallSettings({ recallDatasets: ' docs, wiki ,' }, 'hi', {
				datasetName: 'support',
				datasetId: 'uuid',
			}),
		).toMatchObject({ datasets: ['docs', 'wiki'], datasetIds: undefined });
	});

	it('bounds numeric options an expression may have mangled', () => {
		expect(
			resolveRecallSettings({ recallTopK: Infinity, maxContextChars: 'nope' } as never, 'hi', {
				datasetName: 'd',
			}),
		).toMatchObject({ topK: 5, maxChars: 12000 });
	});

	it('is off when disabled or when the query resolved to nothing', () => {
		expect(
			resolveRecallSettings({ recallContext: false }, 'hi', { datasetName: 'd' }),
		).toBeUndefined();
		expect(resolveRecallSettings({}, '', { datasetName: 'd' })).toBeUndefined();
		expect(resolveRecallSettings({}, '   ', { datasetName: 'd' })).toBeUndefined();
	});
});

describe('CogneeMemory node', () => {
	function supplyContext(
		params: Record<string, unknown>,
		http: (options: Record<string, unknown>) => unknown,
	) {
		const staticData: Record<string, unknown> = {};
		const warnings: string[] = [];
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
			getWorkflowStaticData: () => staticData,
			logger: {
				warn: (message: string) => warnings.push(message),
				info: () => undefined,
				debug: () => undefined,
			},
			addInputData: () => ({ index: 0 }),
			addOutputData: () => undefined,
			helpers: {
				httpRequestWithAuthentication: async (_type: string, options: Record<string, unknown>) =>
					http(options),
			},
		} as unknown as ISupplyDataFunctions;
		return { ctx, staticData, warnings };
	}

	type LcMemory = {
		loadMemoryVariables: (v: Record<string, unknown>) => Promise<{ chat_history: unknown[] }>;
		saveContext: (i: Record<string, unknown>, o: Record<string, unknown>) => Promise<void>;
	};

	it('declares itself as an AI memory sub-node', () => {
		const node = new CogneeMemory();
		expect(node.description.outputs).toEqual(['ai_memory']);
		expect(node.description.inputs).toEqual([]);
		expect(node.description.credentials).toEqual([{ name: 'cogneeApi', required: true }]);
		expect(node.description.codex?.subcategories?.AI).toContain('Memory');
	});

	it('supplies a memory that loads, recalls, saves and promotes through the credential', async () => {
		const http: Array<Record<string, unknown>> = [];
		const { ctx, staticData } = supplyContext(
			{
				sessionId: 'chat-7',
				recallQuery: 'where was Einstein born?',
				options: { datasetName: 'support', windowSize: 2 },
			},
			(options) => {
				http.push(options);
				const url = String(options.url);
				if (options.method === 'GET') return sessionBody;
				if (url.endsWith('/v1/recall'))
					return [onlyContextHit('where was Einstein born?', 'Einstein: born in Ulm.')];
				if (url.endsWith('/v1/improve'))
					return { 'ds-1': { status: 'started', pipeline_run_id: 'run-1' } };
				return { entry_type: 'qa', entry_id: 'q9' };
			},
		);

		const supplied = await new CogneeMemory().supplyData.call(ctx, 0);
		const lcMemory = supplied.response as LcMemory;

		const { chat_history } = await lcMemory.loadMemoryVariables({ input: 'x' });
		expect(chat_history).toHaveLength(5); // window of 2 pairs + recalled context
		expect(http[0]).toMatchObject({
			method: 'GET',
			url: 'https://tenant.example.cognee.ai/api/v1/sessions/chat-7',
			json: true,
		});
		expect(http[1]).toMatchObject({
			method: 'POST',
			url: 'https://tenant.example.cognee.ai/api/v1/recall',
			body: {
				query: 'where was Einstein born?',
				only_context: true,
				scope: 'graph',
				datasets: ['support'],
			},
		});

		await lcMemory.saveContext({ input: 'New question' }, { output: 'New answer' });
		expect(http[2]).toMatchObject({
			method: 'POST',
			url: 'https://tenant.example.cognee.ai/api/v1/remember/entry',
			body: {
				entry: { type: 'qa', question: 'New question', answer: 'New answer', context: '' },
				session_id: 'chat-7',
				dataset_name: 'support',
			},
		});

		expect(supplied.closeFunction).toBeTypeOf('function');
		await supplied.closeFunction?.();
		// n8n's log wrapper re-reads the session after a save for the UI, so
		// requests are matched by URL rather than by position from here on.
		const improve = http.find((o) => String(o.url).endsWith('/v1/improve'));
		expect(improve).toMatchObject({
			method: 'POST',
			url: 'https://tenant.example.cognee.ai/api/v1/improve',
			body: { session_ids: ['chat-7'], dataset_name: 'support', run_in_background: true },
		});
		const record = staticData[STATIC_DATA_KEY] as PromotionState;
		expect(record.lastImproveAt).toBeDefined();
		expect(record.pending).toEqual({});

		// Same workflow, next execution within the interval: nothing is promoted.
		const again = await new CogneeMemory().supplyData.call(ctx, 0);
		await again.closeFunction?.();
		expect(http.filter((o) => String(o.url).endsWith('/v1/improve'))).toHaveLength(1);
	});

	it('sends no recall or improve when both are switched off', async () => {
		const http: Array<Record<string, unknown>> = [];
		const { ctx } = supplyContext(
			{
				sessionId: 'chat-7',
				recallQuery: 'q',
				options: { recallContext: false, promoteMode: 'never' },
			},
			(options) => {
				http.push(options);
				return options.method === 'GET' ? sessionBody : { entry_type: 'qa', entry_id: 'q9' };
			},
		);
		const supplied = await new CogneeMemory().supplyData.call(ctx, 0);
		const lcMemory = supplied.response as LcMemory;
		expect((await lcMemory.loadMemoryVariables({})).chat_history).toHaveLength(4);
		await lcMemory.saveContext({ input: 'q' }, { output: 'a' });
		await supplied.closeFunction?.();
		const paths = new Set(http.map((o) => String(o.url).split('/api')[1]));
		expect(paths).toEqual(new Set(['/v1/sessions/chat-7', '/v1/remember/entry']));
	});

	it('turns a 404 on the session lookup into an empty history and wraps other failures as NodeApiError', async () => {
		const make = (fail: () => never) =>
			supplyContext({ sessionId: 'fresh', options: { recallContext: false } }, () => fail()).ctx;

		const missing = await new CogneeMemory().supplyData.call(
			make(() => {
				throw Object.assign(new Error('not found'), { httpCode: '404' });
			}),
			0,
		);
		expect((await (missing.response as LcMemory).loadMemoryVariables({})).chat_history).toEqual([]);

		const broken = await new CogneeMemory().supplyData.call(
			make(() => {
				throw Object.assign(new Error('forbidden'), { httpCode: '403' });
			}),
			0,
		);
		await expect((broken.response as LcMemory).loadMemoryVariables({})).rejects.toThrow(
			/Forbidden/,
		);
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
