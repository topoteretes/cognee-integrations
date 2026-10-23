/**
 * The receipt ledger: every write this deployment ever accepted, on disk, so a
 * pending write survives a restart and a later `memory_status` or operator
 * `forget` can find the document it produced. SQLite through `node:sqlite`
 * (Node >= 22.13; no native module to compile). `:memory:` is accepted for
 * tests and explicitly reports itself as non-durable.
 */
import { DatabaseSync } from "node:sqlite";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { randomBytes } from "node:crypto";

export type ReceiptStatus = "accepted" | "indexing" | "ready" | "failed" | "unknown" | "deleted";

export const PENDING_STATUSES: readonly ReceiptStatus[] = ["accepted", "indexing", "unknown"];

export interface Receipt {
  id: string;
  requestId: string;
  contentHash: string;
  uploadName: string;
  kind: string;
  dataset: string;
  datasetId: string | null;
  dataId: string | null;
  status: ReceiptStatus;
  error: string | null;
  content: string | null;
  rationale: string | null;
  sourceRef: string | null;
  createdAt: string;
  updatedAt: string;
  lastCheckedAt: string | null;
  checks: number;
}

export interface NewReceipt {
  requestId: string;
  contentHash: string;
  uploadName: string;
  kind: string;
  dataset: string;
  content: string;
  rationale: string | null;
  sourceRef: string | null;
}

export type ReceiptPatch = Partial<Pick<Receipt, "datasetId" | "dataId" | "status" | "error" | "content" | "rationale" | "sourceRef" | "lastCheckedAt">> & {
  bumpChecks?: boolean;
};

interface Row {
  id: string;
  request_id: string;
  content_hash: string;
  upload_name: string;
  kind: string;
  dataset: string;
  dataset_id: string | null;
  data_id: string | null;
  status: string;
  error: string | null;
  content: string | null;
  rationale: string | null;
  source_ref: string | null;
  created_at: string;
  updated_at: string;
  last_checked_at: string | null;
  checks: number;
}

const SCHEMA = `
CREATE TABLE IF NOT EXISTS receipts (
  id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE,
  content_hash TEXT NOT NULL,
  upload_name TEXT NOT NULL,
  kind TEXT NOT NULL,
  dataset TEXT NOT NULL,
  dataset_id TEXT,
  data_id TEXT,
  status TEXT NOT NULL,
  error TEXT,
  content TEXT,
  rationale TEXT,
  source_ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_checked_at TEXT,
  checks INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS receipts_dataset_hash ON receipts (dataset, content_hash);
CREATE INDEX IF NOT EXISTS receipts_status ON receipts (status);
`;


export function newReceiptId(): string {
  return `rcpt_${randomBytes(12).toString("hex")}`;
}

function toReceipt(row: Row): Receipt {
  return {
    id: row.id,
    requestId: row.request_id,
    contentHash: row.content_hash,
    uploadName: row.upload_name,
    kind: row.kind,
    dataset: row.dataset,
    datasetId: row.dataset_id,
    dataId: row.data_id,
    status: row.status as ReceiptStatus,
    error: row.error,
    content: row.content,
    rationale: row.rationale,
    sourceRef: row.source_ref,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    lastCheckedAt: row.last_checked_at,
    checks: Number(row.checks ?? 0),
  };
}

export class ReceiptStore {
  readonly path: string;
  readonly durable: boolean;
  private readonly db: DatabaseSync;
  private readonly clock: () => Date;

  constructor(path: string, options: { clock?: () => Date } = {}) {
    this.path = path;
    this.clock = options.clock ?? (() => new Date());
    this.durable = path !== ":memory:";
    if (this.durable) mkdirSync(dirname(path), { recursive: true });
    this.db = new DatabaseSync(path);
    if (this.durable) this.db.exec("PRAGMA journal_mode = WAL;");
    this.db.exec("PRAGMA foreign_keys = ON;");
    this.db.exec(SCHEMA);
  }

  close(): void {
    this.db.close();
  }

  /** Insert, or return the existing receipt when `requestId` was already used (idempotent replay). */
  create(input: NewReceipt): { receipt: Receipt; created: boolean } {
    const existing = this.getByRequestId(input.requestId);
    if (existing) return { receipt: existing, created: false };
    const timestamp = this.clock().toISOString();
    const id = newReceiptId();
    this.db
      .prepare(
        `INSERT INTO receipts (id, request_id, content_hash, upload_name, kind, dataset, status, content, rationale, source_ref, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?)`,
      )
      .run(id, input.requestId, input.contentHash, input.uploadName, input.kind, input.dataset, input.content, input.rationale, input.sourceRef, timestamp, timestamp);
    const receipt = this.get(id);
    if (!receipt) throw new Error("receipt insert did not persist");
    return { receipt, created: true };
  }

  get(id: string): Receipt | undefined {
    const row = this.db.prepare("SELECT * FROM receipts WHERE id = ?").get(id) as unknown as Row | undefined;
    return row ? toReceipt(row) : undefined;
  }

  getByRequestId(requestId: string): Receipt | undefined {
    const row = this.db.prepare("SELECT * FROM receipts WHERE request_id = ?").get(requestId) as unknown as Row | undefined;
    return row ? toReceipt(row) : undefined;
  }

  /** The newest live (not failed, not deleted) receipt for identical content in a dataset. */
  findLiveByContent(dataset: string, contentHash: string): Receipt | undefined {
    const row = this.db
      .prepare(
        `SELECT * FROM receipts WHERE dataset = ? AND content_hash = ? AND status NOT IN ('failed', 'deleted')
         ORDER BY created_at DESC LIMIT 1`,
      )
      .get(dataset, contentHash) as unknown as Row | undefined;
    return row ? toReceipt(row) : undefined;
  }

  update(id: string, patch: ReceiptPatch): Receipt | undefined {
    const sets: string[] = ["updated_at = ?"];
    const params: unknown[] = [this.clock().toISOString()];
    const columns: Array<[keyof ReceiptPatch, string]> = [
      ["datasetId", "dataset_id"],
      ["dataId", "data_id"],
      ["status", "status"],
      ["error", "error"],
      ["content", "content"],
      ["rationale", "rationale"],
      ["sourceRef", "source_ref"],
      ["lastCheckedAt", "last_checked_at"],
    ];
    for (const [key, column] of columns) {
      if (key in patch) {
        sets.push(`${column} = ?`);
        params.push(patch[key] ?? null);
      }
    }
    if (patch.bumpChecks) sets.push("checks = checks + 1");
    params.push(id);
    this.db.prepare(`UPDATE receipts SET ${sets.join(", ")} WHERE id = ?`).run(...(params as never[]));
    return this.get(id);
  }

  list(filter: { status?: ReceiptStatus | ReceiptStatus[]; limit?: number } = {}): Receipt[] {
    const limit = Math.max(1, Math.min(filter.limit ?? 100, 1000));
    const statuses = filter.status === undefined ? [] : Array.isArray(filter.status) ? filter.status : [filter.status];
    if (statuses.length) {
      const placeholders = statuses.map(() => "?").join(", ");
      const rows = this.db
        .prepare(`SELECT * FROM receipts WHERE status IN (${placeholders}) ORDER BY created_at DESC LIMIT ?`)
        .all(...(statuses as never[]), limit) as unknown as Row[];
      return rows.map(toReceipt);
    }
    const rows = this.db.prepare("SELECT * FROM receipts ORDER BY created_at DESC LIMIT ?").all(limit) as unknown as Row[];
    return rows.map(toReceipt);
  }

  listPending(limit = 500): Receipt[] {
    return this.list({ status: [...PENDING_STATUSES], limit });
  }

  countByStatus(): Record<string, number> {
    const rows = this.db.prepare("SELECT status, COUNT(*) AS n FROM receipts GROUP BY status").all() as unknown as Array<{ status: string; n: number }>;
    const counts: Record<string, number> = {};
    for (const row of rows) counts[row.status] = Number(row.n);
    return counts;
  }

  /** Remove the row entirely (after a verified remote deletion, or operator purge). */
  remove(id: string): boolean {
    const result = this.db.prepare("DELETE FROM receipts WHERE id = ?").run(id);
    return Number(result.changes) > 0;
  }
}
