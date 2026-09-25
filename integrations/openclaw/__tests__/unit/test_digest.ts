/**
 * Memory-hit visibility primitives: footer templating, source attribution,
 * the rolling-week counters, and their on-disk round trip. The tracker is
 * driven with an injected clock so a "week" is a number, not a wait.
 */

import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  DEFAULT_FOOTER_FORMAT,
  DigestTracker,
  WEEK_MS,
  formatDigest,
  formatFooter,
  loadDigestStats,
  saveDigestStats,
  sourceLabel,
  topSources,
} from "../../src/digest";

describe("formatFooter", () => {
  it("renders the default template with count and plural agreement", () => {
    expect(formatFooter(DEFAULT_FOOTER_FORMAT, 3)).toBe("[cognee: 3 memories]");
    expect(formatFooter(DEFAULT_FOOTER_FORMAT, 1)).toBe("[cognee: 1 memory]");
  });

  it("supports a custom template with {sources}", () => {
    expect(formatFooter("(mem {count} from {sources})", 2, ["MEMORY.md", "session"])).toBe(
      "(mem 2 from MEMORY.md, session)",
    );
  });

  it("leaves a template with no placeholders untouched", () => {
    expect(formatFooter("⚡ recalled", 5)).toBe("⚡ recalled");
  });
});

describe("sourceLabel", () => {
  it("prefers a metadata source key and strips path/.txt noise", () => {
    expect(sourceLabel({ id: "1", text: "", score: 1, metadata: { source: "memory/company/handbook.md.txt" } }, "agent"))
      .toBe("handbook.md");
    expect(sourceLabel({ id: "1", text: "", score: 1, metadata: { name: "MEMORY.md" } }, "agent")).toBe("MEMORY.md");
  });

  it("falls back to the caller label when metadata carries nothing usable", () => {
    expect(sourceLabel({ id: "1", text: "", score: 1 }, "agent")).toBe("agent");
    expect(sourceLabel({ id: "1", text: "", score: 1, metadata: { source: "   ", weight: 3 } }, "user")).toBe("user");
  });
});

describe("topSources / formatDigest", () => {
  it("ranks by count then name, capped at three", () => {
    expect(topSources({ b: 2, a: 2, c: 5, d: 1 })).toEqual(["c", "a", "b"]);
  });

  it("renders the digest sentence, omitting sources when unknown", () => {
    expect(formatDigest({ windowStartMs: 0, totalTurns: 120, turnsWithHits: 47, totalHits: 90, sourceCounts: { "session summaries": 30, "MEMORY.md": 17 } }))
      .toBe("[cognee weekly digest] This week cognee found relevant memories on 47 of your agent's 120 turns (top sources: session summaries, MEMORY.md).");
    expect(formatDigest({ windowStartMs: 0, totalTurns: 1, turnsWithHits: 1, totalHits: 1, sourceCounts: {} }))
      .toBe("[cognee weekly digest] This week cognee found relevant memories on 1 of your agent's 1 turn.");
  });
});

describe("DigestTracker", () => {
  let dir: string;
  let path: string;
  let nowMs: number;
  const clock = () => nowMs;

  beforeEach(async () => {
    dir = await mkdtemp(join(tmpdir(), "cognee-digest-"));
    path = join(dir, "digest-stats.json");
    nowMs = 1_000_000;
  });
  afterEach(async () => {
    await rm(dir, { recursive: true, force: true });
  });

  it("counts turns, hits and sources per agent", async () => {
    const t = new DigestTracker({ path, now: clock });
    await t.ready();
    t.recordTurn("will", 0);
    t.recordTurn("will", 2, ["MEMORY.md", "session"]);
    t.recordTurn("will", 1, ["MEMORY.md"]);
    t.recordTurn("ebay", 1, ["agent"]);

    expect(t.get("will")).toEqual({
      windowStartMs: 1_000_000,
      totalTurns: 3,
      turnsWithHits: 2,
      totalHits: 3,
      sourceCounts: { "MEMORY.md": 2, session: 1 },
    });
    expect(t.get("ebay").totalTurns).toBe(1);
  });

  it("does not deliver a digest while the window is open", async () => {
    const t = new DigestTracker({ path, now: clock });
    await t.ready();
    t.recordTurn("will", 3, ["a"]);
    nowMs += WEEK_MS - 1;
    expect(t.takeDueDigest("will")).toBeUndefined();
    expect(t.takeDueDigest("nobody")).toBeUndefined();
  });

  it("delivers exactly once when the week closes, then starts a fresh window", async () => {
    const t = new DigestTracker({ path, now: clock });
    await t.ready();
    t.recordTurn("will", 0);
    t.recordTurn("will", 2, ["MEMORY.md", "MEMORY.md"]);
    nowMs += WEEK_MS;

    const text = t.takeDueDigest("will");
    expect(text).toBe("[cognee weekly digest] This week cognee found relevant memories on 1 of your agent's 2 turns (top sources: MEMORY.md).");
    expect(t.takeDueDigest("will")).toBeUndefined();
    expect(t.get("will")).toMatchObject({ windowStartMs: nowMs, totalTurns: 0, turnsWithHits: 0, lastDigestAtMs: nowMs });
  });

  it("suppresses the digest for a week with zero hits but still rolls the window", async () => {
    const t = new DigestTracker({ path, now: clock });
    await t.ready();
    t.recordTurn("will", 0);
    t.recordTurn("will", 0);
    nowMs += WEEK_MS + 5;

    expect(t.takeDueDigest("will")).toBeNull();
    expect(t.get("will")).toMatchObject({ windowStartMs: nowMs, totalTurns: 0 });
    expect(t.get("will").lastDigestAtMs).toBeUndefined();
  });

  it("persists counters and reloads them, merging turns recorded before the load finished", async () => {
    const t1 = new DigestTracker({ path, now: clock });
    t1.recordTurn("will", 2, ["x"]);
    await t1.flush();
    expect(JSON.parse(await readFile(path, "utf-8")).will.totalHits).toBe(2);

    const t2 = new DigestTracker({ path, now: clock });
    t2.recordTurn("will", 1, ["x"]); // before ready()
    await t2.ready();
    expect(t2.get("will")).toMatchObject({ totalTurns: 2, turnsWithHits: 2, totalHits: 3, sourceCounts: { x: 2 } });
  });

  it("coalesces a burst of turns into a single write and never throws on save failure", async () => {
    const warn = jest.fn();
    const bad = new DigestTracker({ path: join(dir, "not-a-dir-file", "x", "stats.json"), now: clock, warn });
    // Make the parent a file so mkdir -p fails.
    await saveDigestStats({}, join(dir, "not-a-dir-file"));
    for (let i = 0; i < 20; i++) bad.recordTurn("will", 1, ["a"]);
    await bad.flush();
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn.mock.calls[0][0]).toMatch(/digest stats save failed/);
  });
});

describe("loadDigestStats", () => {
  it("returns {} for a missing file and drops malformed entries", async () => {
    const dir = await mkdtemp(join(tmpdir(), "cognee-digest-"));
    const path = join(dir, "s.json");
    expect(await loadDigestStats(path)).toEqual({});

    await saveDigestStats(
      { ok: { windowStartMs: 5, totalTurns: 1, turnsWithHits: 1, totalHits: 1, sourceCounts: { a: 1 } }, bad: "nope" as never, older: { totalTurns: 3 } as never },
      path,
    );
    expect(await loadDigestStats(path)).toEqual({ ok: { windowStartMs: 5, totalTurns: 1, turnsWithHits: 1, totalHits: 1, sourceCounts: { a: 1 } } });
    await rm(dir, { recursive: true, force: true });
  });
});
