import { promises as fs } from "node:fs";
import { dirname, join } from "node:path";
import { homedir } from "node:os";
import { requestErrorStatus } from "./client.js";

// ---------------------------------------------------------------------------
// Capture write buffer — the OpenClaw counterpart of the claude-code/codex
// warm-up buffer (bridge/<scope>.json `pending_entries`).
//
// A capture write (trace or QA) that cannot reach the server is queued here
// and replayed once the server answers again: before session-end improve, on
// the next prompt, and after any successful write. Without it, every turn
// captured while the local server is still booting or briefly down is lost.
//
// Only failures where the request never reached the server are queued: the
// connection could not be opened (refused, unreachable, DNS failure, connect
// timeout) or the server answered 503 while warming up. A reset or closed
// socket, a timeout, or another 5xx can happen after the server stored the
// entry; claude/codex replay those after checking the server's session
// detail, which OpenClaw has no cheap way to do, so they are dropped rather
// than risk a duplicate turn in the graph. Replayed entries leave the file one
// by one, so a crash mid-replay re-sends at most the entry in flight.
// ---------------------------------------------------------------------------

// Next to the plugin's other state (persistence.ts STATE_DIR).
export const WRITE_BUFFER_PATH = join(homedir(), ".openclaw", "memory", "cognee", "pending-entries.json");
export const MAX_BUFFERED_ENTRIES = 500;
export const MAX_BUFFERED_AGE_MS = 7 * 24 * 60 * 60 * 1000;

export type BufferedEntry = {
  datasetName: string;
  sessionId: string;
  kind: string;
  entry: Record<string, unknown>;
  queuedAt: number;
};

type SendFn = (item: BufferedEntry) => Promise<void>;

// Error codes that mean no connection was established, so nothing was sent.
const NOT_CONNECTED_CODES = new Set([
  "ECONNREFUSED",
  "ENOTFOUND",
  "EAI_AGAIN",
  "EHOSTUNREACH",
  "ENETUNREACH",
  "EADDRNOTAVAIL",
  "UND_ERR_CONNECT_TIMEOUT",
]);

function errorCode(error: unknown): string {
  // Node's fetch wraps the socket error: TypeError("fetch failed") with `cause`
  // (sometimes an AggregateError whose `errors` carry the codes).
  for (let e: unknown = error, depth = 0; e && typeof e === "object" && depth < 4; depth++) {
    const r = e as { code?: unknown; cause?: unknown; errors?: unknown };
    if (typeof r.code === "string") return r.code;
    if (Array.isArray(r.errors)) {
      const inner = r.errors.map(errorCode).find((c) => c);
      if (inner) return inner;
    }
    e = r.cause;
  }
  return "";
}

/** True when a failed write certainly never reached the server. */
export function isBufferableWriteError(error: unknown): boolean {
  const status = requestErrorStatus(error);
  if (status !== undefined) return status === 503;
  return NOT_CONNECTED_CODES.has(errorCode(error));
}

export class WriteBuffer {
  // Serializes every read-modify-write of the file within this process, and
  // makes drain single-flight: OpenClaw registers several plugin instances in
  // one gateway, all sharing the instance returned by writeBufferFor().
  private queue: Promise<unknown> = Promise.resolve();
  private draining: Promise<number> | undefined;
  // False only once a drain left the file empty and nothing was enqueued
  // since. Lives here, not in each plugin instance, because the file is
  // shared: an instance that never buffered must still see another's entries.
  // True at start, as an earlier gateway run may have left entries behind.
  private hint = true;

  constructor(private readonly path: string = WRITE_BUFFER_PATH) { }

  private serial<T>(fn: () => Promise<T>): Promise<T> {
    const next = this.queue.then(fn, fn);
    this.queue = next.catch(() => undefined);
    return next;
  }

  private async load(): Promise<BufferedEntry[]> {
    try {
      const parsed = JSON.parse(await fs.readFile(this.path, "utf-8")) as { entries?: unknown };
      return Array.isArray(parsed.entries) ? (parsed.entries as BufferedEntry[]) : [];
    } catch {
      return [];
    }
  }

  private async save(entries: BufferedEntry[]): Promise<void> {
    await fs.mkdir(dirname(this.path), { recursive: true });
    const tmp = `${this.path}.tmp`;
    await fs.writeFile(tmp, JSON.stringify({ entries }), "utf-8");
    await fs.rename(tmp, this.path);
  }

  /** Keep the newest MAX_BUFFERED_ENTRIES, none older than MAX_BUFFERED_AGE_MS. */
  private prune(entries: BufferedEntry[], now = Date.now()): BufferedEntry[] {
    const fresh = entries.filter((e) => now - e.queuedAt <= MAX_BUFFERED_AGE_MS);
    return fresh.length > MAX_BUFFERED_ENTRIES ? fresh.slice(fresh.length - MAX_BUFFERED_ENTRIES) : fresh;
  }

  /** Drop one handled entry; anything enqueued meanwhile is kept. */
  private remove(item: BufferedEntry): Promise<void> {
    return this.serial(async () => {
      const current = await this.load();
      const key = (e: BufferedEntry) => `${e.queuedAt}\u0000${e.sessionId}\u0000${e.kind}\u0000${JSON.stringify(e.entry)}`;
      const i = current.findIndex((e) => key(e) === key(item));
      if (i < 0) return;
      current.splice(i, 1);
      await this.save(current);
    });
  }

  /** False when the file is known to be empty, so a drain can skip the read. */
  get maybeNonEmpty(): boolean {
    return this.hint;
  }

  enqueue(item: Omit<BufferedEntry, "queuedAt">): Promise<void> {
    this.hint = true;
    return this.serial(async () => {
      const entries = await this.load();
      entries.push({ ...item, queuedAt: Date.now() });
      await this.save(this.prune(entries));
    });
  }

  async size(sessionId?: string): Promise<number> {
    const entries = await this.serial(() => this.load());
    return sessionId ? entries.filter((e) => e.sessionId === sessionId).length : entries.length;
  }

  /**
   * Replay queued entries oldest-first until `budgetMs` runs out or the server
   * stops answering. With `sessionId`, only that session's entries are sent
   * (session end); others stay queued. Returns how many were stored.
   *
   * A send that fails with a bufferable error ends the drain (the server is
   * still away) and keeps the rest; any other failure drops that entry, since
   * replaying it can never succeed (a 4xx) or may duplicate it (a timeout).
   */
  drain(send: SendFn, opts: { budgetMs: number; sessionId?: string; onDrop?: (item: BufferedEntry, error: unknown) => void }): Promise<number> {
    if (this.draining) return this.draining.then(() => (opts.sessionId ? this.drain(send, opts) : 0));
    const run = (async () => {
      const deadline = Date.now() + opts.budgetMs;
      const pending = await this.serial(async () => this.prune(await this.load()));
      let stored = 0;
      for (const item of pending) {
        if (Date.now() >= deadline) break;
        if (opts.sessionId && item.sessionId !== opts.sessionId) continue;
        try {
          await send(item);
          stored += 1;
        } catch (error) {
          if (isBufferableWriteError(error)) break;
          opts.onDrop?.(item, error);
        }
        await this.remove(item);
      }
      // Serialized with enqueue, so an entry queued meanwhile keeps the hint set.
      await this.serial(async () => {
        if ((await this.load()).length === 0) this.hint = false;
      });
      return stored;
    })();
    this.draining = run;
    return run.finally(() => { this.draining = undefined; });
  }
}

const buffers = new Map<string, WriteBuffer>();

/** The process-wide buffer for `path`, shared by every plugin instance. */
export function writeBufferFor(path: string = WRITE_BUFFER_PATH): WriteBuffer {
  let buffer = buffers.get(path);
  if (!buffer) { buffer = new WriteBuffer(path); buffers.set(path, buffer); }
  return buffer;
}
