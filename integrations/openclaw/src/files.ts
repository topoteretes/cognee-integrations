import { promises as fs } from "node:fs";
import { createHash } from "node:crypto";
import { join, relative, resolve } from "node:path";
import type { MemoryFile } from "./types.js";
import { DEFAULT_MEMORY_FILE_EXCLUDE, MEMORY_FILE_PATTERNS } from "./config.js";
import { matchGlob } from "./scope.js";

// Openclaw's session-memory hook writes `memory/YYYY-MM-DD-HHMM.md` transcripts;
// skip those — cognee's session cache already holds the same turns and we'd
// double-ingest on /improve.
const SESSION_MEMORY_FILE_RE = /^\d{4}-\d{2}-\d{2}-\d{4}\.md$/;

export function hashText(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

/** True when a workspace-relative path matches one of the exclude globs. */
function isExcluded(relPath: string, exclude: readonly string[]): boolean {
  return exclude.some((pattern) => matchGlob(pattern, relPath));
}

/**
 * Scan the workspace for memory markdown files, skipping any whose
 * workspace-relative path matches a glob in `exclude` (config `memoryFileExclude`).
 */
export async function collectMemoryFiles(
  workspaceDir: string,
  exclude: readonly string[] = DEFAULT_MEMORY_FILE_EXCLUDE,
): Promise<MemoryFile[]> {
  const files: MemoryFile[] = [];

  for (const pattern of MEMORY_FILE_PATTERNS) {
    const target = resolve(workspaceDir, pattern);

    try {
      const stat = await fs.stat(target);

      if (stat.isFile() && target.endsWith(".md")) {
        if (isExcluded(relative(workspaceDir, target), exclude)) continue;
        const content = await fs.readFile(target, "utf-8");
        files.push({
          path: relative(workspaceDir, target),
          absPath: target,
          content,
          hash: hashText(content),
        });
      } else if (stat.isDirectory()) {
        const entries = await scanDir(target, workspaceDir, exclude);
        files.push(...entries);
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
        throw error;
      }
    }
  }

  return files;
}

async function scanDir(
  dir: string,
  workspaceDir: string,
  exclude: readonly string[],
): Promise<MemoryFile[]> {
  const files: MemoryFile[] = [];

  const entries = await fs.readdir(dir, { withFileTypes: true });
  for (const entry of entries) {
    const absPath = join(dir, entry.name);

    if (entry.isDirectory()) {
      const nested = await scanDir(absPath, workspaceDir, exclude);
      files.push(...nested);
    } else if (entry.isFile() && entry.name.endsWith(".md")) {
      if (SESSION_MEMORY_FILE_RE.test(entry.name)) continue;
      const relPath = relative(workspaceDir, absPath);
      if (isExcluded(relPath, exclude)) continue;
      const content = await fs.readFile(absPath, "utf-8");
      files.push({
        path: relPath,
        absPath,
        content,
        hash: hashText(content),
      });
    }
  }

  return files;
}
