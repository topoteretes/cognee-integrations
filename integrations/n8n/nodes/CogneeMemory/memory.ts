/**
 * Cognee-backed chat history for the n8n AI Agent, built on @n8n/ai-node-sdk.
 *
 * Every agent turn is stored as a Cognee session Q&A entry
 * (POST /v1/remember/entry) and read back from the session detail endpoint
 * (GET /v1/sessions/{sessionId}), so the conversation survives restarts and is
 * readable from any Cognee client.
 *
 * Entries stay in the session cache; the typed-entry endpoint does not bridge
 * them into the knowledge graph. To search a whole conversation, including
 * turns older than the loaded window, use the action node's Memory -> Recall
 * with the same Session ID and the session scope.
 *
 * The SDK's WindowedChatMemory supplies the windowing and the turn shape; this
 * module only provides the storage behind it. The HTTP transport is injected so
 * the class stays free of n8n runtime imports and is unit-testable with a fake.
 */
import { BaseChatHistory } from '@n8n/ai-node-sdk';
import type { Message, MessageRole } from '@n8n/ai-node-sdk';

import { DEFAULT_DATASET_NAME, buildRememberEntryPayload } from '../Cognee/payloads';

/** Minimal request contract: `url` is relative to `{baseUrl}/api`. */
export interface CogneeRequestOptions {
	method: 'GET' | 'POST';
	url: string;
	body?: Record<string, unknown>;
	/**
	 * Resolve with `undefined` instead of failing when the server answers 404.
	 * HTTP error shapes stay the transport's concern, so this module never has
	 * to inspect them.
	 */
	allowNotFound?: boolean;
}

export type CogneeRequest = (options: CogneeRequestOptions) => Promise<unknown>;

/** GET /v1/sessions/{id} returns at most this many Q&A pairs. */
export const MAX_SESSION_PAIRS = 20;

/**
 * Marks an entry holding a single message whose role is neither user nor
 * assistant. A Cognee session entry has no role field, so the role rides in
 * `context`, which carries nothing for a message the agent did not generate.
 */
export const ROLE_MARKER = 'n8n:role=';

const MARKED_ROLES = new Set<string>(['system', 'tool']);

export const CLEAR_UNSUPPORTED_MESSAGE =
	'Cognee Memory cannot clear a session: Cognee has no endpoint that deletes a single session. This affects the Chat Memory Manager operations that wipe memory first — Delete Messages, and Insert Messages with Override All Messages. Use a new Session ID to start a fresh conversation, or the Cognee node (Memory → Forget) to remove the dataset the session was remembered into.';

function textOf(message: Message): string {
	return message.content
		.map((part) => ('text' in part && typeof part.text === 'string' ? part.text : ''))
		.filter((text) => text.length > 0)
		.join('\n');
}

function textMessage(role: MessageRole, text: string): Message {
	return { role, content: [{ type: 'text', text }] };
}

/** Q&A entry shape returned inside `qas` by GET /v1/sessions/{sessionId}. */
interface SessionQaEntry {
	question?: unknown;
	answer?: unknown;
	context?: unknown;
}

/**
 * Rebuild the conversation from the session detail response, oldest first.
 *
 * Only the sides actually written are emitted, so an agent turn comes back as
 * one user/assistant pair, and a single message inserted through the Chat
 * Memory Manager comes back as that one message rather than a pair padded with
 * placeholder text.
 */
export function sessionEntriesToMessages(body: unknown): Message[] {
	if (!body || typeof body !== 'object') return [];
	const qas = (body as { qas?: unknown }).qas;
	if (!Array.isArray(qas)) return [];

	const messages: Message[] = [];
	for (const entry of qas as SessionQaEntry[]) {
		if (!entry || typeof entry !== 'object') continue;
		const question = typeof entry.question === 'string' ? entry.question : '';
		const answer = typeof entry.answer === 'string' ? entry.answer : '';
		const context = typeof entry.context === 'string' ? entry.context : '';

		if (context.startsWith(ROLE_MARKER)) {
			const role = context.slice(ROLE_MARKER.length);
			if (answer && MARKED_ROLES.has(role)) {
				messages.push(textMessage(role as MessageRole, answer));
			}
			continue;
		}
		if (question) messages.push(textMessage('user', question));
		if (answer) messages.push(textMessage('assistant', answer));
	}
	return messages;
}

/**
 * POST /v1/remember/entry answers 200 even when the write failed: the body then
 * carries `status: "errored"` and no entry id. Without this check the node would
 * report a turn as remembered when Cognee dropped it.
 */
export function assertEntryStored(response: unknown): void {
	if (!response || typeof response !== 'object') return;
	const body = response as { status?: unknown; entry_id?: unknown; error?: unknown };
	const reportedError = typeof body.error === 'string' && body.error.length > 0;
	// A RememberResult-shaped body with no id means nothing reached the cache.
	const missingId = body.status !== undefined && body.entry_id == null;
	if (body.status === 'errored' || reportedError || missingId) {
		const detail = reportedError ? String(body.error) : `status=${String(body.status)}`;
		throw new Error(`Cognee did not store the conversation turn (${detail})`);
	}
}

export interface CogneeChatHistoryConfig {
	sessionId: string;
	datasetName: string;
	/** Required to target a dataset shared with you; takes precedence over the name. */
	datasetId?: string;
	request: CogneeRequest;
}

/**
 * ChatHistory over a Cognee session. Wrapped in the SDK's `WindowedChatMemory`
 * for the AI Agent's memory port, and driven directly by n8n's Chat Memory
 * Manager node.
 */
export class CogneeChatHistory extends BaseChatHistory {
	private readonly sessionId: string;
	private readonly datasetName: string;
	private readonly datasetId?: string;
	private readonly request: CogneeRequest;

	constructor(config: CogneeChatHistoryConfig) {
		super();
		this.sessionId = config.sessionId;
		this.datasetName = config.datasetName;
		this.datasetId = config.datasetId;
		this.request = config.request;
	}

	async getMessages(): Promise<Message[]> {
		// A 404 means no session record yet — the first turn of a conversation —
		// and yields undefined, which maps to an empty history.
		const body = await this.request({
			method: 'GET',
			url: `/v1/sessions/${encodeURIComponent(this.sessionId)}`,
			allowNotFound: true,
		});
		return sessionEntriesToMessages(body);
	}

	/**
	 * Write one message. Nothing is buffered between calls: n8n's Chat Memory
	 * Manager adds messages one at a time and then discards this instance, so a
	 * message held back waiting for its counterpart would be lost silently.
	 */
	async addMessage(message: Message): Promise<void> {
		await this.addMessages([message]);
	}

	/**
	 * Write a batch as Cognee question/answer entries.
	 *
	 * A user message immediately followed by an assistant message is the normal
	 * agent turn and becomes one paired entry. Anything else is stored as a
	 * half-filled entry, which Cognee accepts, so it reads back as the single
	 * message it was instead of an invented pair. A message carrying no text has
	 * nothing to store and is skipped.
	 */
	async addMessages(messages: Message[]): Promise<void> {
		let index = 0;
		while (index < messages.length) {
			const current = messages[index];
			const next = messages[index + 1];

			if (current.role === 'user' && next?.role === 'assistant') {
				await this.storeEntry(textOf(current), textOf(next));
				index += 2;
				continue;
			}

			const text = textOf(current);
			if (current.role === 'user') await this.storeEntry(text, '');
			else if (current.role === 'assistant') await this.storeEntry('', text);
			// system / tool messages keep their role in the context marker.
			else await this.storeEntry('', text, `${ROLE_MARKER}${current.role}`);
			index += 1;
		}
	}

	async clear(): Promise<void> {
		throw new Error(CLEAR_UNSUPPORTED_MESSAGE);
	}

	/** POST one Q&A entry into the session cache. */
	private async storeEntry(question: string, answer: string, context = ''): Promise<void> {
		if (!question && !answer) return;
		const payload = buildRememberEntryPayload({
			entryType: 'qa',
			sessionId: this.sessionId,
			datasetName: this.datasetName || DEFAULT_DATASET_NAME,
			datasetId: this.datasetId,
			allowPartialQa: true,
			fields: { question, answer, context },
		});
		const response = await this.request({
			method: 'POST',
			url: '/v1/remember/entry',
			body: payload,
		});
		assertEntryStored(response);
	}
}
