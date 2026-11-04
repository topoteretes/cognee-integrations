/**
 * `CogneeInputProcessor` / `CogneeOutputProcessor` — automatic recall/capture
 * for a Mastra agent. Recall runs in `processInput`, capture in
 * `processOutputResult`, fired once per turn. Both read `threadId`/
 * `resourceId` from `args.requestContext` via `parseMemoryRequestContext`,
 * same as Mastra's `SemanticRecall`, so they stay inert without a Mastra
 * `Memory` instance and a thread/resource id passed per call.
 */

import { parseMemoryRequestContext } from "@mastra/core/memory";
import type { MastraDBMessage, MessageList } from "@mastra/core/agent/message-list";
import type { Processor, ProcessInputArgs, ProcessInputResult, ProcessOutputResultArgs } from "@mastra/core/processors";

import type { CogneeMastraConfig, SearchType } from "./types.js";
import { resolveConfig } from "./config.js";
import { resolveScope } from "./scope.js";
import { messageToText, extractMessageText, formatContextBlock, type FormattableMessage } from "./format.js";
import { CircuitBreaker } from "./breaker.js";
import { CogneeApiError, isBreakerError } from "./errors.js";
import { CogneeClient } from "./client.js";

const DEFAULT_MIN_QUERY_LENGTH = 8;
const DEFAULT_SEARCH_TYPE: SearchType = "CHUNKS";
const DEFAULT_TOP_K = 10;
/** Per-call recall timeout; disables client-side retry when set (see client.ts). */
const DEFAULT_RECALL_TIMEOUT_MS = 2_500;
const DEFAULT_RECALL_BUDGET_MS = 4_000;
const DEFAULT_INCLUDE_REFERENCES = true;
const DEFAULT_WRITE_MAX_CHARS = 8_000;
// Mirrors config.ts's defaults as literals so this file stays constructible without
// resolveConfig().

// MastraDBMessage.content is `{content?; parts?}`, not a shape
// extractMessageText understands directly — mirrors SemanticRecall's own unwrap.
function dbMessageText(message: MastraDBMessage): string {
  const content = message.content as unknown;
  if (typeof content === "string") return content;
  if (content && typeof content === "object") {
    const c = content as { content?: unknown; parts?: unknown };
    if (typeof c.content === "string" && c.content) return c.content;
    if (Array.isArray(c.parts)) {
      return extractMessageText(c.parts);
    }
  }
  return "";
}

function toFormattable(message: MastraDBMessage): FormattableMessage {
  return { role: message.role, content: dbMessageText(message) };
}

/**
 * Last user-role message's text, truncated to 2000 chars, or `null`.
 * Doesn't special-case Studio's `role: 'signal'` turn — a query-less turn
 * already fails open below, so missing it just skips one recall.
 */
function extractRecallQuery(messages: MastraDBMessage[]): string | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (!message || message.role !== "user") continue;
    const text = dbMessageText(message).trim();
    if (text) return text.slice(0, 2000);
  }
  return null;
}

/**
 * Races `promise` against a `budgetMs` wall-clock timer so a hung recall
 * can't hold the turn open past `budgetMs`, backstopping the client's own
 * `timeoutMs` (which normally fires first). The losing `promise` is not
 * cancelled (no caller-supplied `AbortController`) but is still handled via `.then` so a late
 * response never becomes an unhandled rejection.
 */
function withBudget<T>(promise: Promise<T>, budgetMs: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`cognee recall exceeded budgetMs (${budgetMs}ms)`));
    }, budgetMs);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

export interface CogneeProcessorOptions {
  /**
   * Shared client; `createCogneeProcessors()` builds one from `config` when not supplied directly.
   */
  client: CogneeClient;
  config: CogneeMastraConfig;
  /**
   * Circuit breaker for the recall path only (write path has none — see `CogneeOutputProcessor`).
   * Defaults to a new instance; tests inject their own.
   */
  breaker?: CircuitBreaker;
}

// ---- CogneeInputProcessor: pre-turn recall ----

/**
 * Recalls from cognee before the model call and injects the result as a
 * system message via `messageList.addSystem(block, "cognee")`. Fails open
 * on every error path — a cognee failure here must never break the turn.
 */
export class CogneeInputProcessor implements Processor {
  readonly id = "cognee-input";
  readonly name = "CogneeInputProcessor";

  private readonly breaker: CircuitBreaker;

  constructor(private readonly opts: CogneeProcessorOptions) {
    this.breaker = opts.breaker ?? new CircuitBreaker();
  }

  async processInput(args: ProcessInputArgs): Promise<ProcessInputResult> {
    const { messages, messageList, requestContext } = args;
    const recallConfig = this.opts.config.recall;

    if (recallConfig?.enabled === false) return messageList;

    // Fail-open boundary: everything below degrades to returning messageList
    // unchanged on any error, including parseMemoryRequestContext itself throwing.
    try {
      // No memory context means no scope to recall into, same no-op as
      // SemanticRecall's own guard.
      const memoryContext = parseMemoryRequestContext(requestContext);
      if (!memoryContext) return messageList;

      const { thread, resourceId } = memoryContext;
      const threadId = thread?.id;

      const query = extractRecallQuery(messages);
      const minQueryLength = recallConfig?.minQueryLength ?? DEFAULT_MIN_QUERY_LENGTH;
      if (!query || query.length < minQueryLength) return messageList;

      // Checked before any network call — an open breaker sends zero requests.
      if (await this.breaker.isOpen()) return messageList;

      const scope = resolveScope(this.opts.config, { threadId, resourceId });
      const timeoutMs = recallConfig?.timeoutMs ?? DEFAULT_RECALL_TIMEOUT_MS;
      const budgetMs = recallConfig?.budgetMs ?? DEFAULT_RECALL_BUDGET_MS;

      const hits = await withBudget(
        this.opts.client.recall(
          {
            query,
            search_type: recallConfig?.searchType ?? DEFAULT_SEARCH_TYPE,
            top_k: recallConfig?.topK ?? DEFAULT_TOP_K,
            only_context: true,
            // resolveScope only resolves a dataset *name*, so scoping goes through `datasets`, not
            // `dataset_ids`.
            datasets: [scope.dataset],
            session_id: scope.sessionId || undefined,
            node_name: scope.nodeNameFilter.length ? scope.nodeNameFilter : undefined,
            include_references: recallConfig?.includeReferences ?? DEFAULT_INCLUDE_REFERENCES,
          },
          // Per-call timeoutMs disables the client's own retry (client.ts).
          { timeoutMs },
        ),
        budgetMs,
      );

      // Awaited (not fire-and-forget) so a failure is visible to the very next call on this breaker
      // instance.
      await this.breaker.recordSuccess();

      const block = formatContextBlock(hits);
      if (block) messageList.addSystem(block, "cognee");
      return messageList;
    } catch (error) {
      // Only a breaker-eligible failure (5xx, or network/timeout with no
      // response) counts against the breaker — a deterministic 4xx (bad key,
      // validation error, wrong baseUrl) is a config problem, not an outage.
      const status = error instanceof CogneeApiError ? error.status : 0;
      if (isBreakerError(status)) {
        await this.breaker.recordFailure(error);
      }
      return messageList;
    }
  }
}

// ---- CogneeOutputProcessor: post-turn capture ----

/**
 * Persists the completed turn into cognee, fire-and-forget, honouring
 * `config.write.mode`. Uses `processOutputResult` — the one output hook
 * that fires once with the full generation result, unlike
 * `processOutputStream`/`processOutputStep`. Fails open like
 * `CogneeInputProcessor`. No circuit breaker: a dropped write just retries
 * next turn, with no user-facing latency to protect.
 */
export class CogneeOutputProcessor implements Processor {
  readonly id = "cognee-output";
  readonly name = "CogneeOutputProcessor";

  constructor(private readonly opts: CogneeProcessorOptions) {}

  // Declared as Promise<MessageList | MastraDBMessage[]>, not
  // Promise<ProcessorMessageResult> — that type is already Promise-wrapped, so nesting it fails
  // structural assignment against Processor's signature.
  async processOutputResult(args: ProcessOutputResultArgs): Promise<MessageList | MastraDBMessage[]> {
    const { messages, messageList, requestContext } = args;
    const passthrough = messageList ?? messages;
    const writeConfig = this.opts.config.write;

    if (writeConfig?.mode === "never") return passthrough;

    // Same fail-open rule as processInput: a throwing parseMemoryRequestContext is treated like a
    // null one.
    let memoryContext: ReturnType<typeof parseMemoryRequestContext>;
    try {
      memoryContext = parseMemoryRequestContext(requestContext);
    } catch {
      return passthrough;
    }
    if (!memoryContext) return passthrough;

    const { thread, resourceId } = memoryContext;
    const threadId = thread?.id;
    if (!threadId) return passthrough;

    // Fire-and-forget: returns `passthrough` immediately while `persistTurn`
    // keeps running; the `.catch` here only guards a synchronous throw before persistTurn's own try
    // block starts.
    void this.persistTurn({ messages, messageList, threadId, resourceId }).catch(() => {});

    return passthrough;
  }

  private async persistTurn(input: {
    messages: MastraDBMessage[];
    messageList: MessageList | undefined;
    threadId: string;
    resourceId?: string;
  }): Promise<void> {
    const writeConfig = this.opts.config.write;
    const maxChars = writeConfig?.maxChars ?? DEFAULT_WRITE_MAX_CHARS;

    try {
      const answer = input.messages
        .map((message) => messageToText(toFormattable(message), maxChars))
        .filter(Boolean)
        .join("\n");

      let question = "";
      if (writeConfig?.mode !== "assistant-only" && input.messageList) {
        const messageList = input.messageList;
        const newUserMessages = messageList.get.input.db().filter((message) => messageList.isNewMessage(message));
        question = newUserMessages
          .map((message) => messageToText(toFormattable(message), maxChars))
          .filter(Boolean)
          .join("\n");
      }

      if (!answer && !question) return; // nothing extractable this turn — not worth a write.

      const scope = resolveScope(this.opts.config, { threadId: input.threadId, resourceId: input.resourceId });

      // rememberEntry() already owns the 404-fallback to remember() internally.
      await this.opts.client.rememberEntry({
        entry: { type: "qa", question, answer },
        dataset_name: scope.dataset,
        session_id: scope.sessionId || undefined,
      });
    } catch {
      // fire-and-forget: never propagate anywhere a caller could observe it.
    }
  }
}

// ---- createCogneeProcessors: one-call wiring ----

export interface CogneeProcessors {
  input: CogneeInputProcessor;
  output: CogneeOutputProcessor;
}

/**
 * Config for `createCogneeProcessors()`/`withCognee()`: `CogneeMastraConfig`
 * plus escape hatches to reuse an existing `CogneeClient` (shared with
 * `createCogneeTools()`) and to inject a breaker (tests).
 */
export interface CogneeProcessorsConfig extends CogneeMastraConfig {
  client?: CogneeClient;
  breaker?: CircuitBreaker;
}

/**
 * Resolves `config` through `resolveConfig()` once, builds a shared
 * `CogneeClient` when `config.client` isn't supplied, and passes the same
 * client to both processors so recall and write share one dataset-id cache.
 * Never throws for a missing credential — `CogneeClient` defers that to the first request.
 */
export function createCogneeProcessors(config: CogneeProcessorsConfig = {}): CogneeProcessors {
  const { client: providedClient, breaker: providedBreaker, ...rest } = config;
  const resolved = resolveConfig(rest);
  const client = providedClient ?? new CogneeClient(resolved);
  const breaker = providedBreaker ?? new CircuitBreaker();
  const opts: CogneeProcessorOptions = { client, config: resolved, breaker };
  return {
    input: new CogneeInputProcessor(opts),
    output: new CogneeOutputProcessor(opts),
  };
}
