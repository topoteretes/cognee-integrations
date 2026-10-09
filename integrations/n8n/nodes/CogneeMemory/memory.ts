/**
 * Cognee-backed chat history for the n8n AI Agent, built on @n8n/ai-node-sdk.
 *
 * Every agent turn is stored as a Cognee session Q&A entry
 * (POST /v1/remember/entry) and read back from the session detail endpoint
 * (GET /v1/sessions/{sessionId}), so the conversation survives restarts and is
 * readable from any Cognee client.
 *
 * Entries stay in the session cache; the typed-entry endpoint does not bridge
 * them into the knowledge graph. `CogneeAgentMemory` does what the Cognee
 * coding-agent plugins do on top of that: before each model call it recalls
 * graph context for the incoming question and hands it to the agent next to
 * the history window, and once per interval it promotes the sessions written
 * since the last run into the graph (POST /v1/improve), so a later
 * conversation under a new session ID can recall what was said.
 *
 * `CogneeChatHistory` is the transcript layer n8n's Chat Memory Manager reads
 * and writes; recalled context lives only in the memory layer the agent reads,
 * so it never lands in the stored session. The HTTP transport is injected so
 * both classes stay free of n8n runtime imports and are unit-testable.
 */
import { BaseChatHistory, BaseChatMemory, WindowedChatMemory } from '@n8n/ai-node-sdk';
import type { Message, MessageRole } from '@n8n/ai-node-sdk';

import {
	DEFAULT_DATASET_NAME,
	buildImprovePayload,
	buildRecallPayload,
	buildRememberEntryPayload,
	summarizeImproveResponse,
} from '../Cognee/payloads';
import { buildContextMessage, extractRecallContext } from './recallContext';

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
 * ChatHistory over a Cognee session: the transcript layer. `CogneeAgentMemory`
 * wraps it (through the SDK's `WindowedChatMemory`) for the AI Agent's memory
 * port, and n8n's Chat Memory Manager node drives it directly.
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

// ---------------------------------------------------------------------------
// Promotion state (lives in the workflow's static data)
// ---------------------------------------------------------------------------

/**
 * Sessions written since their last promotion, grouped by the dataset they
 * must be promoted into, plus when the last promotion ran. Mutated in place:
 * n8n persists the object `getWorkflowStaticData` hands out at the end of a
 * production execution (never a manual one), overwriting the stored copy, so
 * two executions finishing at the same moment can lose each other's pending
 * entries. The nightly promotion workflow is the backstop for that.
 */
export interface PromotionState {
	lastImproveAt?: string;
	/** Pending session IDs with the time of their last write, per target key. */
	pending?: Record<string, Record<string, string>>;
	/** 0.8.0 shape: one bucket with no dataset identity. Migrated on read. */
	pendingSessions?: Record<string, string>;
}

/** The dataset a session is promoted into: by UUID when given, else by name. */
export interface PromotionTarget {
	datasetName: string;
	datasetId?: string;
}

export function targetKey(target: PromotionTarget): string {
	return target.datasetId ? `id:${target.datasetId}` : `name:${target.datasetName}`;
}

export function parseTargetKey(key: string): PromotionTarget {
	return key.startsWith('id:')
		? { datasetName: DEFAULT_DATASET_NAME, datasetId: key.slice(3) }
		: { datasetName: key.replace(/^name:/, '') };
}

/**
 * Pending sessions across all targets at which promotion runs regardless of
 * the interval, so the queue never has to drop anything under normal load.
 */
export const MAX_PENDING_SESSIONS = 1000;
/** Hard cap per target, reached only if promotion keeps failing. */
export const MAX_PENDING_PER_TARGET = 2000;

export type PromotionMode = 'never' | 'interval' | 'eachExecution';

export interface PromotionPolicy {
	mode: PromotionMode;
	/** Minimum time between promotion runs in `interval` mode. */
	intervalMs: number;
}

/** Bring a stored record up to the current shape. The legacy bucket joins the given target. */
export function normalizeState(state: PromotionState, target: PromotionTarget): void {
	state.pending ??= {};
	if (state.pendingSessions) {
		const bucket = (state.pending[targetKey(target)] ??= {});
		for (const [id, at] of Object.entries(state.pendingSessions)) bucket[id] ??= at;
		delete state.pendingSessions;
	}
}

export function pendingCount(state: PromotionState): number {
	return Object.values(state.pending ?? {}).reduce((n, b) => n + Object.keys(b).length, 0);
}

/**
 * Record a write. Returns the session IDs evicted to stay under the per-target
 * cap (oldest first), so the caller can report them; empty under normal load.
 */
export function markSessionPending(
	state: PromotionState,
	target: PromotionTarget,
	sessionId: string,
	nowMs: number,
): string[] {
	normalizeState(state, target);
	const bucket = (state.pending![targetKey(target)] ??= {});
	bucket[sessionId] = new Date(nowMs).toISOString();
	const ids = Object.keys(bucket);
	if (ids.length <= MAX_PENDING_PER_TARGET) return [];
	const evicted = ids
		.sort((a, b) => Date.parse(bucket[a]) - Date.parse(bucket[b]))
		.slice(0, ids.length - MAX_PENDING_PER_TARGET);
	for (const id of evicted) delete bucket[id];
	return evicted;
}

export function promotionDue(
	state: PromotionState,
	policy: PromotionPolicy,
	nowMs: number,
): boolean {
	if (policy.mode === 'never') return false;
	if (policy.mode === 'eachExecution') return true;
	if (pendingCount(state) >= MAX_PENDING_SESSIONS) return true;
	const last = state.lastImproveAt ? Date.parse(state.lastImproveAt) : NaN;
	if (Number.isNaN(last)) return true;
	return nowMs - last >= policy.intervalMs;
}

/** Everything pending, grouped by target, as a snapshot the caller submits. */
export function pendingBatches(
	state: PromotionState,
): Array<{ key: string; sessionIds: string[] }> {
	return Object.entries(state.pending ?? {})
		.filter(([, bucket]) => Object.keys(bucket).length > 0)
		.map(([key, bucket]) => ({ key, sessionIds: Object.keys(bucket) }));
}

/** Acknowledge a submitted snapshot: only those IDs leave the bucket. */
export function acknowledgeSessions(
	state: PromotionState,
	key: string,
	sessionIds: string[],
): void {
	const bucket = state.pending?.[key];
	if (!bucket) return;
	for (const id of sessionIds) delete bucket[id];
	if (Object.keys(bucket).length === 0) delete state.pending![key];
}

// ---------------------------------------------------------------------------
// Agent memory: history window + recalled context + promotion
// ---------------------------------------------------------------------------

export interface RecallSettings {
	/** The incoming question; an empty query skips recall. */
	query: string;
	searchType: string;
	scope: string[];
	datasets: string[];
	datasetIds?: string[];
	topK: number;
	maxChars: number;
	role: 'system' | 'user';
}

export interface PromotionSettings {
	policy: PromotionPolicy;
	target: PromotionTarget;
	/** The persisted record; mutated in place. */
	state: PromotionState;
}

export interface PromotionBatchResult {
	target: PromotionTarget;
	sessionIds: string[];
	/**
	 * `submitted`: accepted, or busy with a rerun promised by the holder, so
	 * the sessions are acknowledged. `busy`: the server's lock was held with no
	 * such promise, so the sessions stay pending for the next execution.
	 * `failed`: request error, sessions stay pending, warning logged.
	 */
	status: 'submitted' | 'busy' | 'failed';
	error?: string;
}

export type PromotionResult =
	| { outcome: 'skipped'; reason: 'disabled' | 'not_due' | 'nothing_pending' }
	| { outcome: 'ran'; batches: PromotionBatchResult[] };

export interface CogneeAgentMemoryConfig {
	chatHistory: CogneeChatHistory;
	sessionId: string;
	windowSize: number;
	request: CogneeRequest;
	recall?: RecallSettings;
	promotion?: PromotionSettings;
	/** Problems that must not fail the agent turn are reported here. */
	warn?: (message: string) => void;
	/** Diagnostics that are only interesting when something looks off. */
	debug?: (message: string) => void;
	now?: () => number;
}

/**
 * The memory the AI Agent talks to. The SDK's `WindowedChatMemory` supplies
 * the transcript behaviour (window, turn shape, clear); this class adds the
 * recalled-context message on load, marks the session for promotion on save,
 * and runs the promotion from the execution's close hook. Recall and
 * promotion fail open: an error is reported through `warn` and the agent
 * still gets its history and its reply.
 */
export class CogneeAgentMemory extends BaseChatMemory {
	readonly chatHistory: CogneeChatHistory;
	private readonly window: WindowedChatMemory;
	private readonly sessionId: string;
	private readonly request: CogneeRequest;
	private readonly recall?: RecallSettings;
	private readonly promotion?: PromotionSettings;
	private readonly warn: (message: string) => void;
	private readonly debug: (message: string) => void;
	private readonly now: () => number;
	/**
	 * One recall per execution: a tool-using turn calls `loadMessages` once per
	 * model round, and the question does not change between rounds.
	 */
	private recalled?: Promise<Message | undefined>;

	constructor(config: CogneeAgentMemoryConfig) {
		super();
		this.chatHistory = config.chatHistory;
		this.window = new WindowedChatMemory(config.chatHistory, { windowSize: config.windowSize });
		this.sessionId = config.sessionId;
		this.request = config.request;
		this.recall = config.recall;
		this.promotion = config.promotion;
		this.warn = config.warn ?? (() => undefined);
		this.debug = config.debug ?? (() => undefined);
		this.now = config.now ?? (() => Date.now());
		if (this.promotion) normalizeState(this.promotion.state, this.promotion.target);
	}

	async loadMessages(): Promise<Message[]> {
		const [history, context] = await Promise.all([this.window.loadMessages(), this.loadContext()]);
		return context ? [...history, context] : history;
	}

	async saveTurn(input: string, output: string): Promise<void> {
		await this.window.saveTurn(input, output);
		if (this.promotion && this.promotion.policy.mode !== 'never') {
			const { state, target } = this.promotion;
			const evicted = markSessionPending(state, target, this.sessionId, this.now());
			if (evicted.length) {
				this.warn(
					`Cognee Memory dropped ${evicted.length} session(s) from the promotion queue for "${target.datasetId ?? target.datasetName}" because promotion keeps failing; promote them with Cognee → Memory → Improve: ${evicted.join(', ')}`,
				);
			}
		}
	}

	async clear(): Promise<void> {
		await this.window.clear();
	}

	/**
	 * Promote the pending sessions into their datasets when the policy says
	 * so, one request per dataset. Called from the execution's close hook,
	 * which n8n runs inside the agent node's execution, before the engine
	 * snapshots static data. The timestamp only advances when every batch was
	 * accepted, so a busy or failing server is retried on the next execution.
	 */
	async promoteIfDue(): Promise<PromotionResult> {
		if (!this.promotion || this.promotion.policy.mode === 'never') {
			return { outcome: 'skipped', reason: 'disabled' };
		}
		const { policy, state } = this.promotion;
		const nowMs = this.now();
		if (!promotionDue(state, policy, nowMs)) return { outcome: 'skipped', reason: 'not_due' };
		const batches = pendingBatches(state);
		if (batches.length === 0) return { outcome: 'skipped', reason: 'nothing_pending' };

		const results: PromotionBatchResult[] = [];
		for (const { key, sessionIds } of batches) {
			const target = parseTargetKey(key);
			try {
				const body = await this.request({
					method: 'POST',
					url: '/v1/improve',
					body: buildImprovePayload({ sessionIds, ...target, runInBackground: true }),
				});
				const summary = summarizeImproveResponse(body);
				const covered = summary.status === 'submitted' || summary.rerunRequested;
				if (covered) acknowledgeSessions(state, key, sessionIds);
				results.push({ target, sessionIds, status: covered ? 'submitted' : 'busy' });
			} catch (error) {
				const message = error instanceof Error ? error.message : String(error);
				this.warn(
					`Cognee Memory could not promote ${sessionIds.length} session(s) into "${target.datasetId ?? target.datasetName}": ${message}`,
				);
				results.push({ target, sessionIds, status: 'failed', error: message });
			}
		}
		if (results.every((r) => r.status === 'submitted')) {
			state.lastImproveAt = new Date(nowMs).toISOString();
		}
		return { outcome: 'ran', batches: results };
	}

	private loadContext(): Promise<Message | undefined> {
		if (!this.recall || !this.recall.query.trim()) return Promise.resolve(undefined);
		this.recalled ??= this.fetchContext(this.recall);
		return this.recalled;
	}

	private async fetchContext(recall: RecallSettings): Promise<Message | undefined> {
		try {
			const body = await this.request({
				method: 'POST',
				url: '/v1/recall',
				body: buildRecallPayload({
					query: recall.query,
					searchType: recall.searchType,
					datasets: recall.datasets,
					topK: recall.topK,
					options: {
						sessionId: this.sessionId,
						scope: recall.scope,
						datasetIds: recall.datasetIds,
						onlyContext: true,
						// Servers that know the option answer with the bare context;
						// older ones return the full prompt, which extractRecallContext trims.
						contextFormat: 'context',
					},
				}),
			});
			const { context, unparsed } = extractRecallContext(body, recall.query, recall.maxChars);
			if (unparsed > 0) {
				this.debug(
					`Cognee Memory injected ${unparsed} recall hit(s) whole because the server's prompt template was not recognised`,
				);
			}
			return context ? buildContextMessage(context, recall.role) : undefined;
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			this.warn(`Cognee Memory recall failed; continuing without recalled context: ${message}`);
			return undefined;
		}
	}
}
