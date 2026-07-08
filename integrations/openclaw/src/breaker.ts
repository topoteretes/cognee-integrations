import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { homedir } from "node:os";

// ---------------------------------------------------------------------------
// Recall circuit breaker — same design and state file as the claude-code and
// codex integrations (~/.cognee-plugin/recall-breaker.json), so all Cognee
// plugins sharing one server trip and recover together. Shape:
//   { failures: number, cooldown_until: epoch-seconds, last_error?: string }
// Opens after `threshold` consecutive breaker-eligible failures; stays open
// for `cooldownMs`, then half-opens (next recall attempt runs).
// ---------------------------------------------------------------------------

export const DEFAULT_BREAKER_PATH = join(homedir(), ".cognee-plugin", "recall-breaker.json");

type BreakerState = {
  failures: number;
  cooldown_until: number;
  last_error?: string;
};

/**
 * A failure should trip the breaker only when it signals an unavailable or
 * overloaded server: network errors, timeouts/aborts, and 5xx responses.
 * 4xx (auth, stale dataset ids) are the caller's problem and must NOT trip —
 * mirrors the claude/codex `_cognee_client.py` rules.
 */
export function isBreakerError(error: unknown): boolean {
  const msg = error instanceof Error ? error.message : String(error);
  const status = /\((\d{3})\)/.exec(msg)?.[1];
  if (status) return status.startsWith("5");
  return true; // no HTTP status → network error, timeout, or abort
}

export class RecallBreaker {
  constructor(
    private readonly threshold: number,
    private readonly cooldownMs: number,
    private readonly path: string = DEFAULT_BREAKER_PATH,
  ) { }

  private async load(): Promise<BreakerState> {
    try {
      const raw = JSON.parse(await readFile(this.path, "utf-8")) as Partial<BreakerState>;
      return {
        failures: typeof raw.failures === "number" ? raw.failures : 0,
        cooldown_until: typeof raw.cooldown_until === "number" ? raw.cooldown_until : 0,
        last_error: typeof raw.last_error === "string" ? raw.last_error : undefined,
      };
    } catch {
      return { failures: 0, cooldown_until: 0 };
    }
  }

  private async save(state: BreakerState): Promise<void> {
    try {
      await mkdir(dirname(this.path), { recursive: true });
      await writeFile(this.path, JSON.stringify(state), "utf-8");
    } catch { /* best-effort */ }
  }

  /** Seconds until the breaker closes again; 0 when recall may proceed. */
  async openForSeconds(): Promise<number> {
    const state = await this.load();
    const remaining = state.cooldown_until - Date.now() / 1000;
    return remaining > 0 ? remaining : 0;
  }

  async recordFailure(message: string): Promise<void> {
    const state = await this.load();
    state.failures += 1;
    state.last_error = message.slice(0, 300);
    if (state.failures >= this.threshold) {
      // Failures are NOT reset on trip (same as claude/codex): after the
      // cooldown the breaker half-opens, and a single failed probe re-trips
      // immediately instead of paying `threshold` slow attempts again.
      state.cooldown_until = Date.now() / 1000 + this.cooldownMs / 1000;
    }
    await this.save(state);
  }

  async recordSuccess(): Promise<void> {
    const state = await this.load();
    if (state.failures === 0 && state.cooldown_until === 0) return; // avoid write churn on the happy path
    await this.save({ failures: 0, cooldown_until: 0 });
  }
}
