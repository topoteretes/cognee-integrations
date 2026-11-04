/**
 * The capture write buffer: what is queued (only writes that certainly never
 * reached the server), and how replay behaves (oldest first, per session at
 * session end, stops while the server is still away, drops what can never
 * succeed).
 */

import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CogneeRequestError } from "../../src/client";
import { MAX_BUFFERED_AGE_MS, MAX_BUFFERED_ENTRIES, WriteBuffer, isBufferableWriteError, type BufferedEntry } from "../../src/write-buffer";

let dir: string;
let buffer: WriteBuffer;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "write-buffer-"));
  buffer = new WriteBuffer(join(dir, "pending.json"));
});
afterEach(() => rmSync(dir, { recursive: true, force: true }));

const entry = (sessionId: string, n: number) => ({ datasetName: "ds", sessionId, kind: "qa", entry: { type: "qa", question: `q${n}`, answer: `a${n}` } });
const refused = () => Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } });

describe("isBufferableWriteError", () => {
  it("queues a refused connection and a 503 (nothing was stored)", () => {
    expect(isBufferableWriteError(refused())).toBe(true);
    expect(isBufferableWriteError(new CogneeRequestError(503, "warming up"))).toBe(true);
  });

  it("queues unreachable hosts, including Node's AggregateError wrapping", () => {
    for (const code of ["ENOTFOUND", "EHOSTUNREACH", "UND_ERR_CONNECT_TIMEOUT"]) {
      expect(isBufferableWriteError(Object.assign(new TypeError("fetch failed"), { cause: { code } }))).toBe(true);
    }
    const aggregate = Object.assign(new TypeError("fetch failed"), { cause: new AggregateError([{ code: "ECONNREFUSED" }]) });
    expect(isBufferableWriteError(aggregate)).toBe(true);
  });

  it("drops a reset or closed connection: the server may have stored the entry before it broke", () => {
    for (const code of ["ECONNRESET", "UND_ERR_SOCKET", "EPIPE"]) {
      expect(isBufferableWriteError(Object.assign(new TypeError("fetch failed"), { cause: { code } }))).toBe(false);
    }
    expect(isBufferableWriteError(new TypeError("fetch failed"))).toBe(false); // no code: unknown, so not replayed
  });

  it("drops timeouts and other statuses (the entry may already be stored, or can never be)", () => {
    expect(isBufferableWriteError(Object.assign(new Error("aborted"), { name: "AbortError" }))).toBe(false);
    expect(isBufferableWriteError(new DOMException("aborted", "AbortError"))).toBe(false);
    expect(isBufferableWriteError(new CogneeRequestError(500, "boom"))).toBe(false);
    expect(isBufferableWriteError(new CogneeRequestError(422, "bad entry"))).toBe(false);
  });
});

describe("WriteBuffer", () => {
  it("replays oldest first and empties the file", async () => {
    await buffer.enqueue(entry("s1", 1));
    await buffer.enqueue(entry("s1", 2));
    const sent: string[] = [];
    const stored = await buffer.drain(async (e) => { sent.push(String(e.entry.question)); }, { budgetMs: 5_000 });
    expect(stored).toBe(2);
    expect(sent).toEqual(["q1", "q2"]);
    expect(await buffer.size()).toBe(0);
  });

  it("stops at the first still-unreachable send and keeps the rest", async () => {
    await buffer.enqueue(entry("s1", 1));
    await buffer.enqueue(entry("s1", 2));
    const send = jest.fn().mockResolvedValueOnce(undefined).mockRejectedValueOnce(refused());
    expect(await buffer.drain(send, { budgetMs: 5_000 })).toBe(1);
    expect(await buffer.size()).toBe(1);
    const left = JSON.parse(readFileSync(join(dir, "pending.json"), "utf-8")).entries as BufferedEntry[];
    expect(left[0].entry.question).toBe("q2");
  });

  it("drops an entry the server rejects, reporting it, and carries on", async () => {
    await buffer.enqueue(entry("s1", 1));
    await buffer.enqueue(entry("s1", 2));
    const onDrop = jest.fn();
    const send = jest.fn().mockRejectedValueOnce(new CogneeRequestError(422, "bad")).mockResolvedValueOnce(undefined);
    expect(await buffer.drain(send, { budgetMs: 5_000, onDrop })).toBe(1);
    expect(onDrop).toHaveBeenCalledTimes(1);
    expect(await buffer.size()).toBe(0);
  });

  it("drains only the given session at session end, leaving others queued", async () => {
    await buffer.enqueue(entry("s1", 1));
    await buffer.enqueue(entry("s2", 2));
    const send = jest.fn(async () => {});
    expect(await buffer.drain(send, { budgetMs: 5_000, sessionId: "s2" })).toBe(1);
    expect(await buffer.size("s1")).toBe(1);
    expect(await buffer.size("s2")).toBe(0);
  });

  it("removes each entry as soon as it is stored, so a crash mid-replay re-sends nothing already stored", async () => {
    await buffer.enqueue(entry("s1", 1));
    await buffer.enqueue(entry("s1", 2));
    const onDisk = () => (JSON.parse(readFileSync(join(dir, "pending.json"), "utf-8")).entries as BufferedEntry[]).map((e) => e.entry.question);
    const seen: unknown[][] = [];
    await buffer.drain(async () => { seen.push(onDisk()); }, { budgetMs: 5_000 });
    // While sending the second entry, the first is already gone from disk.
    expect(seen).toEqual([["q1", "q2"], ["q2"]]);
  });

  it("keeps entries queued while a drain is running", async () => {
    await buffer.enqueue(entry("s1", 1));
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const draining = buffer.drain(async () => { await gate; }, { budgetMs: 5_000 });
    await buffer.enqueue(entry("s1", 2)); // lands mid-drain
    release();
    await draining;
    expect(await buffer.size()).toBe(1);
  });

  it("knows when it is empty, for every caller sharing it", async () => {
    expect(buffer.maybeNonEmpty).toBe(true); // an earlier run may have left entries
    await buffer.drain(jest.fn(async () => {}), { budgetMs: 5_000 });
    expect(buffer.maybeNonEmpty).toBe(false);
    void buffer.enqueue(entry("s1", 1));
    expect(buffer.maybeNonEmpty).toBe(true); // set before the write lands
    await buffer.drain(jest.fn(async () => {}), { budgetMs: 5_000, sessionId: "other" });
    expect(buffer.maybeNonEmpty).toBe(true); // s1's entry is still queued
  });

  it("keeps the hint set when an entry is queued while a drain empties the file", async () => {
    await buffer.enqueue(entry("s1", 1));
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    const draining = buffer.drain(async () => { await gate; }, { budgetMs: 5_000 });
    const queued = buffer.enqueue(entry("s1", 2));
    release();
    await Promise.all([draining, queued]);
    expect(buffer.maybeNonEmpty).toBe(true);
    expect(await buffer.size()).toBe(1);
  });

  it("caps the queue at the newest entries and drops expired ones", async () => {
    for (let i = 0; i < MAX_BUFFERED_ENTRIES + 3; i++) await buffer.enqueue(entry("s1", i));
    expect(await buffer.size()).toBe(MAX_BUFFERED_ENTRIES);

    const now = Date.now();
    jest.spyOn(Date, "now").mockReturnValue(now + MAX_BUFFERED_AGE_MS + 1);
    try {
      expect(await buffer.drain(jest.fn(async () => {}), { budgetMs: 5_000 })).toBe(0);
    } finally {
      jest.restoreAllMocks();
    }
  });
});
