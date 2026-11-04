// ---------------------------------------------------------------------------
// Memory-hit visibility: per-turn footer + weekly digest
//
// Users feel cognee's cost on every turn (recall latency, injected tokens)
// but never see its value — a recall that helped is invisible in the reply.
// This module makes hits visible without adding LLM calls or hot-path I/O:
//
//   * Footer:  when auto-recall injected >= 1 memory into a turn, the final
//              reply gets a one-line trailer, e.g. "[cognee: 3 memories]".
//              Empty turns get nothing.
//   * Digest:  a rolling 7-day window counts non-noise turns, turns with
//              hits, and which memory sources produced the hits. Once the
//              window closes and at least one hit happened, the summary is
//              appended once to the next final reply, then the window resets.
//
// Counters live in ~/.openclaw/memory/cognee/digest-stats.json, keyed by
// normalized agentId. Saves are fire-and-forget and coalesced so a burst of
// turns produces one write, never a stall on the prompt path.
// ---------------------------------------------------------------------------

import { promises as fs } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import type { CogneeSearchResult } from "./types.js";

export const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

/** Resolved at call time (not module load) so the suite-wide homedir sandbox applies. */
export function digestStatsPath(): string {
  return join(homedir(), ".openclaw", "memory", "cognee", "digest-stats.json");
}

// ---------------------------------------------------------------------------
// Footer
// ---------------------------------------------------------------------------

export const DEFAULT_FOOTER_FORMAT = "[cognee: {count} {memories}]";

/**
 * Render the footer template. Placeholders:
 *   {count}    — number of memories injected
 *   {memories} — "memory" or "memories" to agree with {count}
 *   {sources}  — comma-joined distinct source labels ("" when unknown)
 */
export function formatFooter(template: string, count: number, sources: readonly string[] = []): string {
  return template
    .replaceAll("{count}", String(count))
    .replaceAll("{memories}", count === 1 ? "memory" : "memories")
    .replaceAll("{sources}", sources.join(", "));
}

// ---------------------------------------------------------------------------
// Source attribution
// ---------------------------------------------------------------------------

const SOURCE_METADATA_KEYS = ["source", "file_path", "filePath", "name", "document_name", "origin", "type"] as const;

/**
 * Best-effort human label for where a recalled memory came from. Cognee's
 * result metadata is free-form, so probe the usual keys and fall back to the
 * caller-supplied label (typically the memory scope, or "session").
 */
export function sourceLabel(result: CogneeSearchResult, fallback: string): string {
  const meta = result.metadata;
  if (meta && typeof meta === "object") {
    for (const key of SOURCE_METADATA_KEYS) {
      const value = meta[key];
      if (typeof value === "string" && value.trim()) {
        // "memory/company/handbook.md.txt" -> "handbook.md"
        return value.trim().split("/").pop()!.replace(/\.txt$/, "");
      }
    }
  }
  return fallback;
}

// ---------------------------------------------------------------------------
// Weekly stats
// ---------------------------------------------------------------------------

export type AgentDigestStats = {
  /** Epoch ms when the current 7-day window opened. */
  windowStartMs: number;
  /** Non-noise turns where recall was attempted. */
  totalTurns: number;
  /** Turns where >= 1 memory was injected. */
  turnsWithHits: number;
  /** Total memories injected across all turns. */
  totalHits: number;
  /** Source label -> times it produced an injected memory. */
  sourceCounts: Record<string, number>;
  /** Epoch ms of the last digest delivery, if any. */
  lastDigestAtMs?: number;
};

export type DigestStatsFile = Record<string, AgentDigestStats>;

export function emptyAgentStats(nowMs: number): AgentDigestStats {
  return { windowStartMs: nowMs, totalTurns: 0, turnsWithHits: 0, totalHits: 0, sourceCounts: {} };
}

export async function loadDigestStats(path = digestStatsPath()): Promise<DigestStatsFile> {
  try {
    const raw = await fs.readFile(path, "utf-8");
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out: DigestStatsFile = {};
    for (const [agent, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (!value || typeof value !== "object") continue;
      const v = value as Partial<AgentDigestStats>;
      if (typeof v.windowStartMs !== "number") continue;
      out[agent] = {
        windowStartMs: v.windowStartMs,
        totalTurns: typeof v.totalTurns === "number" ? v.totalTurns : 0,
        turnsWithHits: typeof v.turnsWithHits === "number" ? v.turnsWithHits : 0,
        totalHits: typeof v.totalHits === "number" ? v.totalHits : 0,
        sourceCounts: v.sourceCounts && typeof v.sourceCounts === "object" ? { ...v.sourceCounts } : {},
        ...(typeof v.lastDigestAtMs === "number" ? { lastDigestAtMs: v.lastDigestAtMs } : {}),
      };
    }
    return out;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw error;
  }
}

export async function saveDigestStats(stats: DigestStatsFile, path = digestStatsPath()): Promise<void> {
  await fs.mkdir(dirname(path), { recursive: true });
  await fs.writeFile(path, JSON.stringify(stats, null, 2), "utf-8");
}

/** Top-N sources by hit count, ties broken alphabetically for stable output. */
export function topSources(sourceCounts: Record<string, number>, n = 3): string[] {
  return Object.entries(sourceCounts)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, n)
    .map(([label]) => label);
}

/** Render the weekly digest message for one agent's closed window. */
export function formatDigest(stats: AgentDigestStats): string {
  const turns = `${stats.turnsWithHits} of your agent's ${stats.totalTurns} ${stats.totalTurns === 1 ? "turn" : "turns"}`;
  const sources = topSources(stats.sourceCounts);
  const sourcesPart = sources.length > 0 ? ` (top sources: ${sources.join(", ")})` : "";
  return `[cognee weekly digest] This week cognee found relevant memories on ${turns}${sourcesPart}.`;
}

// ---------------------------------------------------------------------------
// Tracker
// ---------------------------------------------------------------------------

export type DigestTrackerOptions = {
  /** Override the stats file location (tests). */
  path?: string;
  /** Clock, injectable for tests. */
  now?: () => number;
  /** Rolling window length; defaults to 7 days. */
  windowMs?: number;
  /** Sink for load/save failures; counting must never throw into the agent loop. */
  warn?: (message: string) => void;
};

/**
 * In-memory counters with lazy load and coalesced, fire-and-forget saves.
 * All public methods are synchronous from the caller's point of view except
 * `ready()`, which resolves once the on-disk state has been read.
 */
export class DigestTracker {
  private stats: DigestStatsFile = {};
  private readonly path: string;
  private readonly now: () => number;
  private readonly windowMs: number;
  private readonly warn?: (message: string) => void;
  private readonly loaded: Promise<void>;
  private saveScheduled = false;
  private saveChain: Promise<void> = Promise.resolve();

  constructor(opts: DigestTrackerOptions = {}) {
    this.path = opts.path ?? digestStatsPath();
    this.now = opts.now ?? Date.now;
    this.windowMs = opts.windowMs ?? WEEK_MS;
    this.warn = opts.warn;
    this.loaded = loadDigestStats(this.path)
      .then((s) => {
        // Turns recorded before the load finished are merged, not dropped.
        for (const [agent, disk] of Object.entries(s)) {
          const mem = this.stats[agent];
          this.stats[agent] = mem ? mergeStats(disk, mem) : disk;
        }
      })
      .catch((e) => this.warn?.(`cognee-openclaw: digest stats load failed: ${String(e)}`));
  }

  /** Resolves once persisted counters have been merged in. */
  ready(): Promise<void> {
    return this.loaded;
  }

  /** Resolves once any pending save has flushed (tests / shutdown). */
  flush(): Promise<void> {
    return this.saveChain;
  }

  /** Snapshot for one agent (never mutates). */
  get(agentId: string): AgentDigestStats {
    return structuredClone(this.stats[agentId] ?? emptyAgentStats(this.now()));
  }

  /**
   * Record one non-noise turn. `hitCount` is the number of memories injected
   * (0 when recall found nothing); `sources` are labels for those memories.
   */
  recordTurn(agentId: string, hitCount: number, sources: readonly string[] = []): void {
    const s = (this.stats[agentId] ??= emptyAgentStats(this.now()));
    s.totalTurns += 1;
    if (hitCount > 0) {
      s.turnsWithHits += 1;
      s.totalHits += hitCount;
      for (const label of sources) s.sourceCounts[label] = (s.sourceCounts[label] ?? 0) + 1;
    }
    this.scheduleSave();
  }

  /**
   * If this agent's window has closed, return the digest text to deliver (or
   * null when the week had zero hits) and open a fresh window. Returns
   * undefined while the window is still open. Idempotent per window.
   */
  takeDueDigest(agentId: string): string | null | undefined {
    const s = this.stats[agentId];
    if (!s) return undefined;
    const now = this.now();
    if (now - s.windowStartMs < this.windowMs) return undefined;

    const text = s.turnsWithHits > 0 ? formatDigest(s) : null;
    const fresh = emptyAgentStats(now);
    fresh.lastDigestAtMs = text ? now : s.lastDigestAtMs;
    this.stats[agentId] = fresh;
    this.scheduleSave();
    return text;
  }

  private scheduleSave(): void {
    if (this.saveScheduled) return;
    this.saveScheduled = true;
    // Coalesce: wait for the initial load and the current microtask burst,
    // then write once. Never awaited by hook code.
    this.saveChain = this.saveChain
      .then(() => this.loaded)
      .then(() => new Promise<void>((r) => setImmediate(r)))
      .then(() => {
        this.saveScheduled = false;
        return saveDigestStats(this.stats, this.path);
      })
      .catch((e) => {
        this.saveScheduled = false;
        this.warn?.(`cognee-openclaw: digest stats save failed: ${String(e)}`);
      });
  }
}

/** Combine on-disk counters with turns recorded before the disk read finished. */
function mergeStats(disk: AgentDigestStats, mem: AgentDigestStats): AgentDigestStats {
  const sourceCounts = { ...disk.sourceCounts };
  for (const [k, v] of Object.entries(mem.sourceCounts)) sourceCounts[k] = (sourceCounts[k] ?? 0) + v;
  return {
    windowStartMs: Math.min(disk.windowStartMs, mem.windowStartMs),
    totalTurns: disk.totalTurns + mem.totalTurns,
    turnsWithHits: disk.turnsWithHits + mem.turnsWithHits,
    totalHits: disk.totalHits + mem.totalHits,
    sourceCounts,
    ...(disk.lastDigestAtMs !== undefined ? { lastDigestAtMs: disk.lastDigestAtMs } : {}),
  };
}
