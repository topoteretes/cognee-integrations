import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, renameSync, rmSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

export const hash = (value: string) => createHash("sha256").update(value).digest("hex");
export function sessionId(directory: string, nativeId: string): string {
  let canonical = resolve(directory);
  try { canonical = realpathSync(canonical); } catch { /* An offline workspace still has a stable absolute path. */ }
  return `opencode_${hash(canonical).slice(0, 16)}_${hash(nativeId).slice(0, 24)}`;
}
export function scrub(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(scrub);
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, /(?:api[_-]?key|password|secret|authorization|access[_-]?token)/i.test(key) ? "[REDACTED]" : scrub(item)]));
  if (typeof value !== "string") return value;
  return value
    .replace(/-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)/g, "[REDACTED PRIVATE KEY]")
    .replace(/\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+/gi, "[REDACTED AUTH]")
    .replace(/\b(?:sk-[\w-]{12,}|gh[pousr]_[\w]{12,}|AKIA[A-Z0-9]{16})\b/g, "[REDACTED TOKEN]")
    .replace(/((?:api[_-]?key|password|secret|access[_-]?token)["']?\s*[:=]\s*["']?)[^\s"',;]+/gi, "$1[REDACTED]")
    .replace(/(\w+:\/\/)[^\s/@:]+:[^\s/@]+@/g, "$1[REDACTED]@");
}
export function allowedTool(tool: string, args: unknown, allow: string[]): boolean {
  if (allow.length && !allow.includes(tool)) return false;
  const visit = (value: unknown): boolean => {
    if (!value || typeof value !== "object") return true;
    return Object.entries(value).every(([key, item]) => {
      if (/^(?:file_?path|path|paths|filename|notebook_path)$/i.test(key)) {
        return [item].flat().every((path) => typeof path !== "string" || !/(?:^|[\\/])(?:\.env(?:\.[^\\/]*)?|\.ssh|\.aws|\.npmrc|\.netrc|id_rsa[^\\/]*|credentials[^\\/]*|secrets[^\\/]*)(?:[\\/]|$)|\.(?:pem|key|p12|pfx)$/i.test(path));
      }
      return visit(item);
    });
  };
  return visit(args);
}
export type Entry = { type: "qa" | "trace"; [key: string]: unknown };
type Pending = { id: string; session: string; entry: Entry; uncertain?: boolean };
type State = { pending: Pending[]; seen: string[] };

/** Atomic local journal. Ambiguous writes are reconciled, never blindly repeated. */
export class Outbox {
  readonly file: string;
  private running?: Promise<void>;
  constructor(namespace: string, root = join(homedir(), ".cognee-plugin", "opencode")) {
    mkdirSync(root, { recursive: true, mode: 0o700 });
    this.file = join(root, hash(namespace) + ".json");
  }
  private read(): State {
    try { const state = JSON.parse(readFileSync(this.file, "utf8")); if (!Array.isArray(state.pending) || !Array.isArray(state.seen)) throw new Error("Invalid outbox"); return state; }
    catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return { pending: [], seen: [] }; throw error; }
  }
  private write(state: State): void {
    const temp = this.file + "." + randomUUID();
    writeFileSync(temp, JSON.stringify(state), { mode: 0o600 });
    renameSync(temp, this.file);
  }
  private lock<T>(run: () => T): T {
    const path = this.file + ".lock";
    try { mkdirSync(path, { mode: 0o700 }); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      // Serialize stale-owner recovery, then re-read the owner under that lock.
      // Two recoverers must never remove a lock that the other just acquired.
      const recovery = path + ".recovery";
      mkdirSync(recovery, { mode: 0o700 });
      try {
        const owner = Number(readFileSync(join(path, "pid"), "utf8"));
        if (!Number.isSafeInteger(owner) || owner <= 0) throw new Error("Outbox lock owner unknown");
        try { process.kill(owner, 0); throw new Error("Outbox busy"); }
        catch (probe) { if ((probe as NodeJS.ErrnoException).code !== "ESRCH") throw probe; }
        rmSync(path, { recursive: true }); mkdirSync(path, { mode: 0o700 });
        writeFileSync(join(path, "pid"), String(process.pid), { mode: 0o600 });
      } finally { rmSync(recovery, { recursive: true }); }
    }
    writeFileSync(join(path, "pid"), String(process.pid), { mode: 0o600 });
    try { return run(); } finally { rmSync(path, { recursive: true }); }
  }
  enqueue(id: string, session: string, entry: Entry): void {
    this.lock(() => {
      const state = this.read();
      if (state.seen.includes(id) || state.pending.some((item) => item.id === id)) return;
      state.pending.push({ id, session, entry: scrub(entry) as Entry }); this.write(state);
    });
  }
  status(): { pending: number; uncertain: number; saved: number } {
    const state = this.read();
    return { pending: state.pending.length, uncertain: state.pending.filter((item) => item.uncertain).length, saved: state.seen.length };
  }
  flush(send: (item: Pending) => Promise<void>, committed: (item: Pending) => Promise<boolean>): Promise<void> {
    if (this.running) return this.running;
    this.running = (async () => {
      // Network outside the short file lock. Mark uncertain durably BEFORE sending.
      for (;;) {
        const item = this.lock(() => { const state = this.read(); const next = state.pending[0]; if (!next) return undefined; const copy = { ...next }; next.uncertain = true; this.write(state); return copy; });
        if (!item) return;
        if (item.uncertain) { if (!await committed(item)) return; }
        else {
          try { await send(item); }
          catch (error) {
            // An explicit validation/auth rejection proves this request was not stored.
            if (/(?:request|login) failed \((?:400|401|403|404|422|429)\)/.test(String(error))) {
              this.lock(() => { const state = this.read(); const pending = state.pending.find((p) => p.id === item.id); if (pending) pending.uncertain = false; this.write(state); });
            }
            throw error;
          }
        }
        this.lock(() => { const state = this.read(); state.pending = state.pending.filter((p) => p.id !== item.id); if (!state.seen.includes(item.id)) state.seen.push(item.id); this.write(state); });
      }
    })().finally(() => { this.running = undefined; });
    return this.running;
  }
}
