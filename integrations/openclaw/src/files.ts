import { promises as fs } from "node:fs";
import { createHash } from "node:crypto";
import { join, relative, resolve } from "node:path";
import type { MemoryFile } from "./types.js";
import { MEMORY_FILE_PATTERNS } from "./config.js";

export function hashText(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

/**
 * Scan the workspace for memory markdown files.
 */
export async function collectMemoryFiles(workspaceDir: string): Promise<MemoryFile[]> {
  const files: MemoryFile[] = [];

  for (const pattern of MEMORY_FILE_PATTERNS) {
    const target = resolve(workspaceDir, pattern);

    try {
      const stat = await fs.stat(target);

      if (stat.isFile() && target.endsWith(".md")) {
        const content = await fs.readFile(target, "utf-8");
        files.push({
          path: relative(workspaceDir, target),
          absPath: target,
          content,
          hash: hashText(content),
        });
      } else if (stat.isDirectory()) {
        const entries = await scanDir(target, workspaceDir);
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

async function scanDir(dir: string, workspaceDir: string): Promise<MemoryFile[]> {
  const files: MemoryFile[] = [];

  const entries = await fs.readdir(dir, { withFileTypes: true });
  for (const entry of entries) {
    const absPath = join(dir, entry.name);

    if (entry.isDirectory()) {
      const nested = await scanDir(absPath, workspaceDir);
      files.push(...nested);
    } else if (entry.isFile() && entry.name.endsWith(".md")) {
      const content = await fs.readFile(absPath, "utf-8");
      files.push({
        path: relative(workspaceDir, absPath),
        absPath,
        content,
        hash: hashText(content),
      });
    }
  }

  return files;
}
