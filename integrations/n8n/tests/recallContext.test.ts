import { describe, expect, it } from 'vitest';

import {
	GUIDANCE_TITLE,
	buildContextMessage,
	capContext,
	extractRecallContext,
	recallHitText,
	splitOnlyContext,
} from '../nodes/CogneeMemory/recallContext';

const QUERY = 'what sport does the user play';

/** The prompt shape cognee >= 1.6 returns for a completion search with only_context. */
function serverPrompt(context: string, { history = '', guidance = '' } = {}): string {
	const head = history ? `${history}\n\n` : '';
	const tail = guidance ? `\`\n\n${GUIDANCE_TITLE}\n${guidance}` : '`';
	return `${head}The question is: \`${QUERY}\`\nAnswer using this sectioned context. Keep the answer brief.\n\nContext:\n\`${context}${tail}`;
}

describe('splitOnlyContext', () => {
	it('keeps only the retrieved context from a server prompt', () => {
		const context =
			'## Relevant passages\nAndrej plays tennis on Tuesdays.\n---\nHe also likes padel.';
		expect(splitOnlyContext(serverPrompt(context), QUERY)).toEqual({ context, parsed: true });
	});

	it('drops the conversation history before the question and the guidance after it', () => {
		const context = '## Relevant passages\nfact';
		const text = serverPrompt(context, {
			history: 'Question: hi\nAnswer: hello',
			guidance: 'Be brief.',
		});
		expect(splitOnlyContext(text, QUERY)).toEqual({ context, parsed: true });
	});

	it('survives backticks inside the context', () => {
		const context = 'Run `npm test` then `npm run lint`.';
		expect(splitOnlyContext(serverPrompt(context), QUERY).context).toBe(context);
	});

	it('fails open on anything it cannot read', () => {
		expect(splitOnlyContext('bare context from an older server', QUERY)).toEqual({
			context: 'bare context from an older server',
			parsed: false,
		});
		// The marker is rendered from a different question.
		expect(splitOnlyContext(serverPrompt('x'), 'another question').parsed).toBe(false);
		// Marker present, but neither a closing backtick nor a guidance block.
		expect(
			splitOnlyContext(`The question is: \`${QUERY}\`\nContext:\n\`cut off`, QUERY).parsed,
		).toBe(false);
	});
});

describe('recallHitText', () => {
	it('reads strings, graph hits and session hits', () => {
		expect(recallHitText('plain')).toBe('plain');
		expect(recallHitText({ kind: 'graph_completion', text: 'from graph' })).toBe('from graph');
		expect(recallHitText({ source: 'session', question: 'q', answer: 'from session' })).toBe(
			'from session',
		);
		expect(recallHitText(null)).toBe('');
	});
});

describe('capContext', () => {
	it('cuts at a line break and marks the cut', () => {
		const text = `${'a'.repeat(60)}\n${'b'.repeat(60)}\n${'c'.repeat(60)}`;
		const capped = capContext(text, 130);
		expect(capped).toBe(`${'a'.repeat(60)}\n${'b'.repeat(60)}\n…`);
		expect(capContext(text, 1000)).toBe(text);
		expect(capContext(text, 0)).toBe(text);
	});

	it('cuts hard when the only line break is too early', () => {
		const text = `ab\n${'c'.repeat(100)}`;
		expect(capContext(text, 50)).toBe(`ab\n${'c'.repeat(47)}\n…`);
	});
});

describe('extractRecallContext', () => {
	it('joins the distinct contexts of several hits', () => {
		const body = [
			{ text: serverPrompt('first') },
			{ text: serverPrompt('first') },
			{ text: serverPrompt('second') },
		];
		expect(extractRecallContext(body, QUERY, 12000)).toEqual({
			context: 'first\n\n---\n\nsecond',
			unparsed: 0,
		});
	});

	it('returns an empty string for nothing retrieved', () => {
		expect(extractRecallContext([], QUERY, 100).context).toBe('');
		expect(extractRecallContext(undefined, QUERY, 100).context).toBe('');
		expect(extractRecallContext([{ text: serverPrompt('   ') }], QUERY, 100).context).toBe('');
	});

	it('accepts a single hit that is not wrapped in an array', () => {
		expect(extractRecallContext(serverPrompt('solo'), QUERY, 100)).toEqual({
			context: 'solo',
			unparsed: 0,
		});
	});

	it('counts a prompt it could not trim, but not a bare context from a newer server', () => {
		const bare = extractRecallContext(['## Relevant passages\nbare context'], QUERY, 100);
		expect(bare).toEqual({ context: '## Relevant passages\nbare context', unparsed: 0 });
		const foreign = extractRecallContext([serverPrompt('x')], 'a different question', 100);
		expect(foreign.unparsed).toBe(1);
		expect(foreign.context).toContain('The question is:');
	});
});

describe('buildContextMessage', () => {
	it('wraps the context with a preamble matching the role', () => {
		const system = buildContextMessage('fact', 'system');
		expect(system.role).toBe('system');
		expect((system.content[0] as { text: string }).text).toMatch(
			/long-term memory[\s\S]*\n\nfact$/,
		);

		const user = buildContextMessage('fact', 'user');
		expect(user.role).toBe('user');
		expect((user.content[0] as { text: string }).text).toMatch(
			/not a message from the user[\s\S]*fact$/,
		);
	});
});
