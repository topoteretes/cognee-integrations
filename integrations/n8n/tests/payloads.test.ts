import { describe, expect, it } from 'vitest';

import {
	RECALL_AUTO,
	buildForgetPayload,
	buildImprovePayload,
	buildRecallPayload,
	buildRememberEntryPayload,
	parseGraphModel,
	simplifyRecallResult,
	summarizeImproveResponse,
} from '../nodes/Cognee/payloads';

describe('buildRecallPayload', () => {
	it('sends the minimal payload with search_type and query', () => {
		expect(
			buildRecallPayload({ query: ' what is cognee? ', searchType: 'HYBRID_COMPLETION' }),
		).toEqual({ query: 'what is cognee?', search_type: 'HYBRID_COMPLETION' });
	});

	it('maps AUTO to a null search_type so the server routes the query', () => {
		expect(buildRecallPayload({ query: 'q', searchType: RECALL_AUTO }).search_type).toBeNull();
	});

	it('rejects an empty query', () => {
		expect(() => buildRecallPayload({ query: '  ', searchType: 'CHUNKS' })).toThrow(
			/Query is required/,
		);
	});

	it('forwards datasets, top_k and every option under its snake_case key', () => {
		const payload = buildRecallPayload({
			query: 'q',
			searchType: 'GRAPH_COMPLETION',
			datasets: ['a', ' ', 'b'],
			topK: 7.9,
			options: {
				datasetIds: ['11111111-1111-1111-1111-111111111111'],
				sessionId: 'chat-1',
				scope: ['graph', 'session', 'graph'],
				onlyContext: true,
				contextFormat: 'prompt',
				contextProfile: 'agent',
				nodeName: ['team-a'],
				systemPrompt: 'Be brief.',
				includeReferences: true,
				verbose: true,
			},
		});
		expect(payload).toEqual({
			query: 'q',
			search_type: 'GRAPH_COMPLETION',
			datasets: ['a', 'b'],
			dataset_ids: ['11111111-1111-1111-1111-111111111111'],
			top_k: 7,
			session_id: 'chat-1',
			scope: ['graph', 'session'],
			only_context: true,
			context_format: 'prompt',
			context_profile: 'agent',
			node_name: ['team-a'],
			system_prompt: 'Be brief.',
			include_references: true,
			verbose: true,
		});
	});

	it('sends a single scope as a string and omits context_format without only_context', () => {
		const payload = buildRecallPayload({
			query: 'q',
			searchType: 'CHUNKS',
			options: { scope: ['session'], contextFormat: 'prompt', onlyContext: false },
		});
		expect(payload.scope).toBe('session');
		expect(payload).not.toHaveProperty('only_context');
		expect(payload).not.toHaveProperty('context_format');
	});
});

describe('simplifyRecallResult', () => {
	it('flattens graph hits to source/text plus kind and dataset', () => {
		const row = simplifyRecallResult({
			source: 'graph',
			kind: 'graph_completion',
			search_type: 'GRAPH_COMPLETION',
			text: 'Paris is the capital.',
			score: 0.9,
			dataset_name: 'geo',
			dataset_id: 'd1',
			raw: {},
		});
		expect(row).toMatchObject({
			source: 'graph',
			text: 'Paris is the capital.',
			kind: 'graph_completion',
			score: 0.9,
			dataset_name: 'geo',
		});
		expect(row.raw).toBeDefined();
	});

	it('uses the answer as text for session entries', () => {
		const row = simplifyRecallResult({
			source: 'session',
			question: 'Q?',
			answer: 'A.',
			qa_id: 'qa-1',
			time: '2026-01-01T00:00:00Z',
			context: '',
		});
		expect(row).toMatchObject({ source: 'session', text: 'A.', question: 'Q?', qa_id: 'qa-1' });
	});

	it('handles trace, session_context, tools and system entries', () => {
		expect(
			simplifyRecallResult({
				source: 'trace',
				origin_function: 'f',
				status: 'success',
				memory_context: 'ctx',
			}),
		).toMatchObject({ source: 'trace', text: 'ctx', origin_function: 'f' });
		expect(
			simplifyRecallResult({
				source: 'trace',
				origin_function: 'f',
				status: 'success',
				method_return_value: { a: 1 },
			}),
		).toMatchObject({ text: '{"a":1}' });
		expect(
			simplifyRecallResult({ source: 'session_context', content: 'c', context_profile: 'qa' }),
		).toMatchObject({
			text: 'c',
			context_profile: 'qa',
		});
		expect(
			simplifyRecallResult({
				source: 'tools',
				tool_name: 'text_to_sql',
				question: 'q',
				text: '42',
				success: true,
			}),
		).toMatchObject({ source: 'tools', text: '42', tool_name: 'text_to_sql' });
		expect(
			simplifyRecallResult({ source: 'system', status: 'warming_up', text: 'still warming' }),
		).toMatchObject({
			source: 'system',
			text: 'still warming',
		});
	});

	it('wraps bare strings and unknown shapes', () => {
		expect(simplifyRecallResult('plain context')).toEqual({
			source: 'context',
			text: 'plain context',
		});
		expect(simplifyRecallResult(null)).toEqual({ source: 'unknown', text: '' });
		expect(simplifyRecallResult({ foo: 'bar' })).toMatchObject({
			source: 'unknown',
			text: '{"foo":"bar"}',
		});
	});
});

describe('buildRememberEntryPayload', () => {
	it('builds a qa entry with defaults', () => {
		expect(
			buildRememberEntryPayload({
				entryType: 'qa',
				sessionId: 'chat-1',
				fields: { question: 'Q?', answer: 'A.' },
			}),
		).toEqual({
			entry: { type: 'qa', question: 'Q?', answer: 'A.', context: '' },
			session_id: 'chat-1',
			dataset_name: 'main_dataset',
		});
	});

	it('includes optional qa fields and a dataset id', () => {
		const payload = buildRememberEntryPayload({
			entryType: 'qa',
			sessionId: 's',
			datasetName: 'notes',
			datasetId: 'd-1',
			fields: {
				question: 'Q',
				answer: 'A',
				context: 'ctx',
				feedbackText: 'good',
				feedbackScore: 4.6,
			},
		});
		expect(payload.dataset_id).toBe('d-1');
		expect(payload.dataset_name).toBe('notes');
		expect(payload.entry).toEqual({
			type: 'qa',
			question: 'Q',
			answer: 'A',
			context: 'ctx',
			feedback_text: 'good',
			feedback_score: 5,
		});
	});

	it('requires session id, question and answer for qa', () => {
		expect(() =>
			buildRememberEntryPayload({
				entryType: 'qa',
				sessionId: '',
				fields: { question: 'q', answer: 'a' },
			}),
		).toThrow(/Session ID is required/);
		expect(() =>
			buildRememberEntryPayload({ entryType: 'qa', sessionId: 's', fields: { question: 'q' } }),
		).toThrow(/Question and Answer are required/);
	});

	it('builds a trace entry, parsing JSON strings for structured fields', () => {
		const payload = buildRememberEntryPayload({
			entryType: 'trace',
			sessionId: 's',
			fields: {
				originFunction: 'search_codebase',
				status: 'error',
				methodParams: '{"q": "x"}',
				methodReturnValue: { hits: 0 },
				memoryQuery: 'mq',
				memoryContext: 'mc',
				errorMessage: 'boom',
			},
		});
		expect(payload.entry).toEqual({
			type: 'trace',
			origin_function: 'search_codebase',
			status: 'error',
			method_params: { q: 'x' },
			method_return_value: { hits: 0 },
			memory_query: 'mq',
			memory_context: 'mc',
			error_message: 'boom',
		});
	});

	it('rejects invalid JSON in trace fields and a missing origin function', () => {
		expect(() =>
			buildRememberEntryPayload({
				entryType: 'trace',
				sessionId: 's',
				fields: { originFunction: 'f', methodParams: '{bad' },
			}),
		).toThrow(/Method Params must be valid JSON/);
		expect(() =>
			buildRememberEntryPayload({ entryType: 'trace', sessionId: 's', fields: {} }),
		).toThrow(/Origin Function is required/);
	});

	it('builds a feedback entry and requires text or score', () => {
		expect(
			buildRememberEntryPayload({
				entryType: 'feedback',
				sessionId: 's',
				fields: { qaId: 'qa-1', feedbackScore: 2 },
			}).entry,
		).toEqual({ type: 'feedback', qa_id: 'qa-1', feedback_score: 2 });
		expect(() =>
			buildRememberEntryPayload({
				entryType: 'feedback',
				sessionId: 's',
				fields: { qaId: 'qa-1' },
			}),
		).toThrow(/Feedback Text and\/or Feedback Score/);
		expect(() =>
			buildRememberEntryPayload({
				entryType: 'feedback',
				sessionId: 's',
				fields: { feedbackText: 't' },
			}),
		).toThrow(/QA ID is required/);
	});
});

describe('buildForgetPayload', () => {
	it('forgets a dataset by name or by id', () => {
		expect(buildForgetPayload({ mode: 'dataset', datasetBy: 'name', datasetName: 'docs' })).toEqual(
			{ dataset: 'docs' },
		);
		expect(buildForgetPayload({ mode: 'dataset', datasetBy: 'id', datasetId: 'd-1' })).toEqual({
			dataset_id: 'd-1',
		});
	});

	it('forgets a single data item and supports memory_only', () => {
		expect(
			buildForgetPayload({
				mode: 'dataItem',
				datasetBy: 'name',
				datasetName: 'docs',
				dataId: 'x-1',
				memoryOnly: true,
			}),
		).toEqual({ dataset: 'docs', data_id: 'x-1', memory_only: true });
	});

	it('requires the identifier matching the chosen mode', () => {
		expect(() =>
			buildForgetPayload({ mode: 'dataset', datasetBy: 'name', datasetName: ' ' }),
		).toThrow(/Dataset Name is required/);
		expect(() => buildForgetPayload({ mode: 'dataset', datasetBy: 'id' })).toThrow(
			/Dataset ID is required/,
		);
		expect(() =>
			buildForgetPayload({ mode: 'dataItem', datasetBy: 'name', datasetName: 'd' }),
		).toThrow(/Data ID is required/);
	});

	it('refuses to forget everything without explicit confirmation', () => {
		expect(() => buildForgetPayload({ mode: 'everything' })).toThrow(
			/Enable the confirmation toggle/,
		);
		expect(buildForgetPayload({ mode: 'everything', confirmEverything: true })).toEqual({
			everything: true,
		});
	});
});

describe('parseGraphModel', () => {
	it('returns undefined for empty input', () => {
		expect(parseGraphModel(undefined)).toBeUndefined();
		expect(parseGraphModel('')).toBeUndefined();
		expect(parseGraphModel('  {}  ')).toBeUndefined();
		expect(parseGraphModel({})).toBeUndefined();
	});

	it('parses a JSON string or passes an object through', () => {
		const model = { title: 'CompanyGraph', type: 'object', properties: {} };
		expect(parseGraphModel(JSON.stringify(model))).toEqual(model);
		expect(parseGraphModel(model)).toEqual(model);
	});

	it('rejects invalid JSON, non-objects and a missing title', () => {
		expect(() => parseGraphModel('{bad')).toThrow(/Graph Model must be valid JSON/);
		expect(() => parseGraphModel('[1]')).toThrow(/must be a JSON object/);
		expect(() => parseGraphModel('{"type": "object"}')).toThrow(/top-level "title"/);
		expect(() => parseGraphModel({ title: '' })).toThrow(/top-level "title"/);
	});
});

describe('buildImprovePayload', () => {
	it('sends deduplicated session ids, the dataset and background mode', () => {
		expect(
			buildImprovePayload({ sessionIds: [' a ', 'b', 'a', ''], datasetName: ' support ' }),
		).toEqual({ session_ids: ['a', 'b'], dataset_name: 'support', run_in_background: true });
	});

	it('falls back to the default dataset, adds the id, and honours foreground mode', () => {
		expect(
			buildImprovePayload({ sessionIds: ['a'], datasetId: 'uuid', runInBackground: false }),
		).toEqual({
			session_ids: ['a'],
			dataset_name: 'main_dataset',
			dataset_id: 'uuid',
			run_in_background: false,
		});
	});

	it('requires at least one session id', () => {
		expect(() => buildImprovePayload({ sessionIds: [] })).toThrow(/At least one Session ID/);
		expect(() => buildImprovePayload({ sessionIds: [' '] })).toThrow(/At least one Session ID/);
	});
});

describe('summarizeImproveResponse', () => {
	it('reads the per-dataset map cognee >= 1.4 answers', () => {
		expect(
			summarizeImproveResponse({
				'ds-1': { status: 'started', pipeline_run_id: 'run-1', extra: true },
				'ds-2': { status: 'completed' },
				note: 'ignored',
			}),
		).toEqual({
			status: 'submitted',
			rerunRequested: false,
			datasets: {
				'ds-1': { status: 'started', pipelineRunId: 'run-1' },
				'ds-2': { status: 'completed', pipelineRunId: undefined },
			},
		});
	});

	it('reads the flat shape of older servers', () => {
		expect(
			summarizeImproveResponse({ status: 'started', pipeline_run_id: 'run-9', dataset_id: 'ds-9' }),
		).toEqual({
			status: 'submitted',
			rerunRequested: false,
			datasets: { 'ds-9': { status: 'started', pipelineRunId: 'run-9' } },
		});
	});

	it('reads the cognee 1.6 flat shape and its lock-held skip', () => {
		const running = {
			dataset_id: 'ds-6',
			dataset_name: 'n8n_test',
			session_ids: ['s'],
			stages: [],
			background: true,
			status: 'running',
		};
		expect(summarizeImproveResponse(running)).toEqual({
			status: 'submitted',
			rerunRequested: false,
			datasets: { 'ds-6': { status: 'running', pipelineRunId: undefined } },
		});
		const held = {
			...running,
			status: 'skipped',
			stages: [
				{ stage: 'feedback_weights', status: 'skipped', reason: 'lock_held' },
				{ stage: 'persist_session_qa', status: 'skipped', reason: 'lock_held' },
			],
		};
		expect(summarizeImproveResponse(held)).toMatchObject({ status: 'busy', rerunRequested: false });
		// A session-level collision where the holder was asked to run again covers the submission.
		expect(summarizeImproveResponse({ ...held, rerun_requested: true })).toMatchObject({
			status: 'busy',
			rerunRequested: true,
		});
		const partial = { ...held, stages: [held.stages[0], { stage: 'x', status: 'completed' }] };
		expect(summarizeImproveResponse(partial).status).toBe('submitted');
	});

	it('reports an empty object as busy and anything else as submitted', () => {
		expect(summarizeImproveResponse({})).toEqual({
			status: 'busy',
			rerunRequested: false,
			datasets: {},
		});
		expect(summarizeImproveResponse(null)).toEqual({
			status: 'submitted',
			rerunRequested: false,
			datasets: {},
		});
		expect(summarizeImproveResponse('ok')).toEqual({
			status: 'submitted',
			rerunRequested: false,
			datasets: {},
		});
	});
});
