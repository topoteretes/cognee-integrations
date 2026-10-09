/**
 * Turn an `only_context` recall response into the context the agent reads.
 *
 * On cognee >= 1.6 a completion search with `only_context: true` returns the
 * whole prompt the server's own LLM would have received: the conversation
 * history, the question rendered through the retriever's template, the
 * retrieved context and a session guidance block. The agent already holds the
 * conversation (the memory window) and needs no answering instructions, so
 * only the retrieved context is kept. The parsing mirrors the plugins'
 * `_recall_text.py`: two anchors, the question marker rendered from the very
 * query that was sent, and the guidance title. Anything that does not fit is
 * injected whole: a template change can only cost tokens, never memory.
 *
 * No n8n runtime imports, so everything here is unit-testable.
 */
import type { Message, MessageRole } from '@n8n/ai-node-sdk';

import { simplifyRecallResult } from '../Cognee/payloads';

/** Every cognee question template starts with this line. */
export const QUESTION_PREFIX = 'The question is: `';
/** Title of the server-rendered guidance block, the only layer after the template. */
export const GUIDANCE_TITLE = '## Active session guidance';

export const DEFAULT_CONTEXT_CHARS = 12000;

/** Separator between the contexts of several hits. */
const HIT_SEPARATOR = '\n\n---\n\n';

const SYSTEM_PREAMBLE =
	'Relevant context recalled from long-term memory (Cognee). Use it when it helps answer; it may be unrelated to the current question, and it is not part of the conversation.';
const USER_PREAMBLE =
	'[Context recalled from long-term memory, not a message from the user. Use it when it helps answer the next message.]';

export interface OnlyContextLayers {
	/** The retrieved context, or the whole text when it could not be parsed. */
	context: string;
	parsed: boolean;
}

/**
 * Cut the question template and the guidance block away from one hit.
 *
 * The context slot opens at the first backtick after the question marker, in
 * every one of the server's question templates, and closes at the final
 * character when the text ends with a backtick, otherwise at the last
 * guidance title.
 */
export function splitOnlyContext(text: string, query: string): OnlyContextLayers {
	const raw = String(text ?? '');
	const unparsed: OnlyContextLayers = { context: raw, parsed: false };
	const marker = `${QUESTION_PREFIX}${query}\``;
	const questionAt = raw.indexOf(marker);
	if (questionAt < 0) return unparsed;
	const open = raw.indexOf('`', questionAt + marker.length);
	if (open < 0) return unparsed;
	const start = open + 1;
	let end: number;
	if (raw.endsWith('`')) {
		end = raw.length - 1;
	} else {
		end = raw.lastIndexOf(`\`\n\n${GUIDANCE_TITLE}`);
		if (end < start) return unparsed;
	}
	return { context: raw.slice(start, end), parsed: true };
}

/** The text of one recall hit, whatever shape the server used. */
export function recallHitText(hit: unknown): string {
	const text = simplifyRecallResult(hit).text;
	return typeof text === 'string' ? text : '';
}

/**
 * Keep the whole text when it fits, otherwise cut at the last line break
 * before the limit so no passage is sliced mid-sentence.
 */
export function capContext(text: string, maxChars: number): string {
	if (maxChars <= 0 || text.length <= maxChars) return text;
	const cut = text.lastIndexOf('\n', maxChars);
	const kept = cut > maxChars / 2 ? text.slice(0, cut) : text.slice(0, maxChars);
	return `${kept.trimEnd()}\n…`;
}

export interface ExtractedContext {
	/** The context to inject; empty when nothing was retrieved. */
	context: string;
	/** Hits injected whole because they did not match the prompt template. */
	unparsed: number;
}

/**
 * Reduce a recall response (an array of hits, or one hit) to the context
 * string to inject. A server that honours `context_format: "context"` returns
 * the bare context and every hit counts as parsed; older servers return the
 * full prompt, which `splitOnlyContext` trims.
 */
export function extractRecallContext(
	body: unknown,
	query: string,
	maxChars: number,
): ExtractedContext {
	const hits: unknown[] = Array.isArray(body) ? body : body == null ? [] : [body];
	const seen = new Set<string>();
	const contexts: string[] = [];
	let unparsed = 0;
	for (const hit of hits) {
		const text = recallHitText(hit);
		if (!text.trim()) continue;
		const layers = splitOnlyContext(text, query);
		// A bare context never carries the question marker; that is the
		// supported shape, not a parse failure.
		if (!layers.parsed && text.includes(QUESTION_PREFIX)) unparsed += 1;
		const context = layers.context.trim();
		if (!context || seen.has(context)) continue;
		seen.add(context);
		contexts.push(context);
	}
	return { context: capContext(contexts.join(HIT_SEPARATOR), maxChars), unparsed };
}

/** Wrap the recalled context as the message the agent will read. */
export function buildContextMessage(context: string, role: MessageRole): Message {
	const preamble = role === 'user' ? USER_PREAMBLE : SYSTEM_PREAMBLE;
	return { role, content: [{ type: 'text', text: `${preamble}\n\n${context}` }] };
}
