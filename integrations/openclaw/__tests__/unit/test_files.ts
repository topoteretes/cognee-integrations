/**
 * `collectMemoryFiles` / `hashText` (src/files.ts) against a real workspace.
 *
 * This is what decides which of a user's files become memory, so it is worth
 * driving over an actual directory tree rather than a mocked `fs`: the behaviour
 * that matters is which paths the walk reaches and which it skips, and a mock only
 * ever confirms the calls the test already predicted.
 *
 * Contract:
 *   * `MEMORY.md` at the root and every `.md` under `memory/` are collected,
 *     recursively;
 *   * non-markdown files are ignored, so a stray binary or lockfile in the memory
 *     directory cannot be ingested;
 *   * OpenClaw's own session transcripts (`memory/YYYY-MM-DD-HHMM.md`) are skipped
 *     — cognee's session cache already holds those turns, and ingesting them again
 *     would double-count every conversation on the next `/improve`;
 *   * a missing workspace is empty, not an error (ENOENT is swallowed; anything
 *     else propagates);
 *   * `path` is workspace-relative while `absPath` is absolute, and `hash` is a
 *     content hash — the pair is what makes change detection work.
 */

import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { collectMemoryFiles, hashText } from "../../src/files";

let workspace: string;

beforeEach(async () => {
  workspace = await mkdtemp(join(tmpdir(), "cognee-openclaw-files-"));
});
afterEach(async () => {
  await rm(workspace, { recursive: true, force: true });
});

/** Write a file inside the workspace, creating parent directories. */
async function put(relPath: string, content = "# note\n"): Promise<void> {
  const abs = join(workspace, relPath);
  await mkdir(join(abs, ".."), { recursive: true });
  await writeFile(abs, content, "utf-8");
}

/** Collected `path` values, sorted — the identity assertions all use this. */
async function collectedPaths(): Promise<string[]> {
  return (await collectMemoryFiles(workspace)).map((f) => f.path).sort();
}

describe("hashText", () => {
  it("is a stable sha256 hex digest", () => {
    expect(hashText("hello")).toBe(
      "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
    );
  });

  it("differs for content that differs by one character", () => {
    // Change detection is only as good as this: a hash that collided on small
    // edits would make the sync silently skip modified files.
    expect(hashText("note a")).not.toBe(hashText("note b"));
  });
});

describe("collectMemoryFiles", () => {
  it("returns nothing for a workspace with no memory files", async () => {
    await expect(collectMemoryFiles(workspace)).resolves.toEqual([]);
  });

  it("returns nothing for a workspace that does not exist", async () => {
    // ENOENT is swallowed deliberately: a fresh install has neither MEMORY.md nor
    // memory/, and the plugin must not fail its first sync over that.
    await expect(collectMemoryFiles(join(workspace, "nope"))).resolves.toEqual([]);
  });

  it("collects MEMORY.md at the workspace root", async () => {
    await put("MEMORY.md");
    await expect(collectedPaths()).resolves.toEqual(["MEMORY.md"]);
  });

  it("collects markdown under memory/, recursively", async () => {
    await put("memory/tools.md");
    await put("memory/nested/deeper/decisions.md");
    await expect(collectedPaths()).resolves.toEqual([
      join("memory", "nested", "deeper", "decisions.md"),
      join("memory", "tools.md"),
    ]);
  });

  it("ignores non-markdown files in the memory directory", async () => {
    // A user's memory dir is a real directory that can accumulate anything;
    // ingesting a lockfile or an image would waste a cognify and pollute the graph.
    await put("memory/notes.md");
    await put("memory/data.json", "{}");
    await put("memory/image.png", "\x89PNG");
    await put("memory/README.txt", "hi");

    await expect(collectedPaths()).resolves.toEqual([join("memory", "notes.md")]);
  });

  it("skips OpenClaw's own session transcripts", async () => {
    // `memory/YYYY-MM-DD-HHMM.md` is written by OpenClaw's session-memory hook.
    // Cognee's session cache already holds those turns, so collecting them would
    // re-ingest every conversation on the next improve.
    await put("memory/2026-08-17-1432.md");
    await put("memory/2026-01-01-0000.md");
    await put("memory/real-note.md");

    await expect(collectedPaths()).resolves.toEqual([join("memory", "real-note.md")]);
  });

  it("keeps markdown whose name only resembles a transcript", async () => {
    // The pattern is anchored, so these are genuine notes rather than transcripts —
    // a looser match would silently drop a user's file.
    await put("memory/2026-08-17.md");
    await put("memory/2026-08-17-1432-retro.md");
    await put("memory/notes-2026-08-17-1432.md");

    await expect(collectedPaths()).resolves.toHaveLength(3);
  });

  it("reports relative path, absolute path and a content hash together", async () => {
    await put("memory/tools.md", "# tools\nripgrep\n");
    const [file] = await collectMemoryFiles(workspace);

    expect(file.path).toBe(join("memory", "tools.md"));
    expect(file.absPath).toBe(join(workspace, "memory", "tools.md"));
    expect(file.content).toBe("# tools\nripgrep\n");
    expect(file.hash).toBe(hashText("# tools\nripgrep\n"));
  });

  it("collects the root file and the directory together", async () => {
    await put("MEMORY.md");
    await put("memory/tools.md");
    await expect(collectedPaths()).resolves.toEqual(["MEMORY.md", join("memory", "tools.md")]);
  });

  it("ignores a MEMORY.md that is a directory rather than a file", async () => {
    // The walk checks isFile() before reading; without that a directory named
    // MEMORY.md would throw EISDIR and fail the whole sync.
    await mkdir(join(workspace, "MEMORY.md"), { recursive: true });
    await expect(collectMemoryFiles(workspace)).resolves.toEqual([]);
  });
});
