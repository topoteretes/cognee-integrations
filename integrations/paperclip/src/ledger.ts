/**
 * The ingestion ledger: one row per (company, source type, source id,
 * revision). It is the durable outbox the plan asks for, kept in the plugin's
 * own database namespace (`migrations/001_memory_ledger.sql`) so a unique
 * constraint, not a read-then-write on `ctx.state`, guarantees one row per
 * revision across replayed and reordered events.
 *
 * `PluginDbLedger` runs SQL through `ctx.db`; `InMemoryLedger` has identical
 * semantics for tests and for the SDK harness (whose `ctx.db` records calls but
 * stores nothing).
 */
import { randomUUID } from "node:crypto";
import type { SourceType } from "./records.js";

export type LedgerStatus =
  | "pending" // queued, not yet sent
  | "accepted" // sent; the backend stored the upload, graph not confirmed
  | "indexing" // document listed; cognify pipeline running
  | "ready" // indexed; recall can return it
  | "uncertain" // write timed out or lost the connection; reconcile resolves it
  | "failed" // terminal; an operator can inspect
  | "conflict" // backend conflict or retirement failure; needs reconciliation
  | "superseded" // an older revision awaiting verified retirement
  | "retired" // older revision deleted on the backend and verified gone
  | "revoked"; // never sent: revoked by cleanup or disable

export const LIVE_STATUSES: readonly LedgerStatus[] = ["pending", "accepted", "indexing", "ready", "uncertain", "conflict"];
export const PENDING_READINESS: readonly LedgerStatus[] = ["accepted", "indexing", "uncertain"];

export interface LedgerRow {
  id: string;
  companyId: string;
  projectId: string;
  sourceType: SourceType;
  sourceId: string;
  revision: string;
  dataset: string;
  uploadName: string;
  content: string | null;
  sourceRef: string;
  link: string | null;
  status: LedgerStatus;
  datasetId: string | null;
  dataId: string | null;
  error: string | null;
  attempts: number;
  createdAt: string;
  updatedAt: string;
  lastCheckedAt: string | null;
}

export interface NewLedgerRow {
  companyId: string;
  projectId: string;
  sourceType: SourceType;
  sourceId: string;
  revision: string;
  dataset: string;
  uploadName: string;
  content: string;
  sourceRef: string;
  link: string | null;
}

export type LedgerPatch = Partial<Pick<LedgerRow, "status" | "datasetId" | "dataId" | "error" | "content" | "lastCheckedAt">> & { bumpAttempts?: boolean };

export interface LedgerFilter {
  companyId?: string;
  projectId?: string;
  dataset?: string;
  status?: LedgerStatus | LedgerStatus[];
  limit?: number;
}

export interface Ledger {
  /** Insert; returns the existing row unchanged when the unique key already exists. */
  upsert(input: NewLedgerRow): Promise<{ row: LedgerRow; created: boolean }>;
  get(id: string): Promise<LedgerRow | undefined>;
  /** Every live row for the same source other than `revision` (the ones a new revision replaces). */
  otherRevisions(companyId: string, sourceType: SourceType, sourceId: string, revision: string): Promise<LedgerRow[]>;
  update(id: string, patch: LedgerPatch): Promise<LedgerRow | undefined>;
  list(filter?: LedgerFilter): Promise<LedgerRow[]>;
  countByStatus(companyId?: string): Promise<Record<string, number>>;
}

export interface Clock {
  (): Date;
}

export class InMemoryLedger implements Ledger {
  private readonly rows = new Map<string, LedgerRow>();
  constructor(private readonly clock: Clock = () => new Date()) {}

  private key(input: Pick<NewLedgerRow, "companyId" | "sourceType" | "sourceId" | "revision">): string {
    return `${input.companyId}\u0000${input.sourceType}\u0000${input.sourceId}\u0000${input.revision}`;
  }

  async upsert(input: NewLedgerRow): Promise<{ row: LedgerRow; created: boolean }> {
    const key = this.key(input);
    for (const row of this.rows.values()) if (this.key(row) === key) return { row, created: false };
    const now = this.clock().toISOString();
    const row: LedgerRow = { id: randomUUID(), ...input, status: "pending", datasetId: null, dataId: null, error: null, attempts: 0, createdAt: now, updatedAt: now, lastCheckedAt: null };
    this.rows.set(row.id, row);
    return { row, created: true };
  }

  async get(id: string): Promise<LedgerRow | undefined> {
    return this.rows.get(id);
  }

  async otherRevisions(companyId: string, sourceType: SourceType, sourceId: string, revision: string): Promise<LedgerRow[]> {
    return [...this.rows.values()].filter((row) => row.companyId === companyId && row.sourceType === sourceType && row.sourceId === sourceId && row.revision !== revision && LIVE_STATUSES.includes(row.status));
  }

  async update(id: string, patch: LedgerPatch): Promise<LedgerRow | undefined> {
    const row = this.rows.get(id);
    if (!row) return undefined;
    const { bumpAttempts, ...fields } = patch;
    const next: LedgerRow = { ...row, ...fields, attempts: row.attempts + (bumpAttempts ? 1 : 0), updatedAt: this.clock().toISOString() };
    this.rows.set(id, next);
    return next;
  }

  async list(filter: LedgerFilter = {}): Promise<LedgerRow[]> {
    const statuses = filter.status === undefined ? null : Array.isArray(filter.status) ? filter.status : [filter.status];
    const out = [...this.rows.values()]
      .filter((row) => (filter.companyId ? row.companyId === filter.companyId : true))
      .filter((row) => (filter.projectId ? row.projectId === filter.projectId : true))
      .filter((row) => (filter.dataset ? row.dataset === filter.dataset : true))
      .filter((row) => (statuses ? statuses.includes(row.status) : true))
      .sort((a, b) => (a.createdAt < b.createdAt ? -1 : a.createdAt > b.createdAt ? 1 : 0));
    return out.slice(0, Math.max(1, Math.min(filter.limit ?? 500, 5000)));
  }

  async countByStatus(companyId?: string): Promise<Record<string, number>> {
    const counts: Record<string, number> = {};
    for (const row of this.rows.values()) {
      if (companyId && row.companyId !== companyId) continue;
      counts[row.status] = (counts[row.status] ?? 0) + 1;
    }
    return counts;
  }
}

/** The subset of `ctx.db` the SQL ledger needs. */
export interface PluginDb {
  query<T = Record<string, unknown>>(sql: string, params?: unknown[]): Promise<T[]>;
  execute(sql: string, params?: unknown[]): Promise<{ rowCount: number }>;
}

export const LEDGER_TABLE = "memory_ledger";

function toRow(raw: Record<string, unknown>): LedgerRow {
  return {
    id: String(raw.id),
    companyId: String(raw.company_id),
    projectId: String(raw.project_id),
    sourceType: String(raw.source_type) as SourceType,
    sourceId: String(raw.source_id),
    revision: String(raw.revision),
    dataset: String(raw.dataset),
    uploadName: String(raw.upload_name),
    content: raw.content === null || raw.content === undefined ? null : String(raw.content),
    sourceRef: String(raw.source_ref ?? ""),
    link: raw.link === null || raw.link === undefined ? null : String(raw.link),
    status: String(raw.status) as LedgerStatus,
    datasetId: raw.dataset_id === null || raw.dataset_id === undefined ? null : String(raw.dataset_id),
    dataId: raw.data_id === null || raw.data_id === undefined ? null : String(raw.data_id),
    error: raw.error === null || raw.error === undefined ? null : String(raw.error),
    attempts: Number(raw.attempts ?? 0),
    createdAt: raw.created_at instanceof Date ? raw.created_at.toISOString() : String(raw.created_at ?? ""),
    updatedAt: raw.updated_at instanceof Date ? raw.updated_at.toISOString() : String(raw.updated_at ?? ""),
    lastCheckedAt: raw.last_checked_at ? (raw.last_checked_at instanceof Date ? raw.last_checked_at.toISOString() : String(raw.last_checked_at)) : null,
  };
}

/**
 * Ledger over the plugin's Postgres namespace. The host restricts `query` to
 * SELECT and `execute` to INSERT/UPDATE/DELETE inside the namespace; the table
 * is created by the manifest-declared migration. `ON CONFLICT DO NOTHING` on
 * the unique key makes replayed and reordered events land exactly one row.
 */
export class PluginDbLedger implements Ledger {
  constructor(private readonly db: PluginDb, private readonly clock: Clock = () => new Date()) {}

  async upsert(input: NewLedgerRow): Promise<{ row: LedgerRow; created: boolean }> {
    const id = randomUUID();
    const now = this.clock().toISOString();
    const result = await this.db.execute(
      `INSERT INTO ${LEDGER_TABLE} (id, company_id, project_id, source_type, source_id, revision, dataset, upload_name, content, source_ref, link, status, attempts, created_at, updated_at)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'pending', 0, $12, $12)
       ON CONFLICT (company_id, source_type, source_id, revision) DO NOTHING`,
      [id, input.companyId, input.projectId, input.sourceType, input.sourceId, input.revision, input.dataset, input.uploadName, input.content, input.sourceRef, input.link, now],
    );
    const rows = await this.db.query(
      `SELECT * FROM ${LEDGER_TABLE} WHERE company_id = $1 AND source_type = $2 AND source_id = $3 AND revision = $4 LIMIT 1`,
      [input.companyId, input.sourceType, input.sourceId, input.revision],
    );
    const found = rows[0] ? toRow(rows[0]) : undefined;
    if (!found) {
      // The harness's db stores nothing; report the row we tried to insert so callers can proceed.
      return { row: { id, ...input, status: "pending", datasetId: null, dataId: null, error: null, attempts: 0, createdAt: now, updatedAt: now, lastCheckedAt: null }, created: result.rowCount > 0 };
    }
    return { row: found, created: result.rowCount > 0 };
  }

  async get(id: string): Promise<LedgerRow | undefined> {
    const rows = await this.db.query(`SELECT * FROM ${LEDGER_TABLE} WHERE id = $1 LIMIT 1`, [id]);
    return rows[0] ? toRow(rows[0]) : undefined;
  }

  async otherRevisions(companyId: string, sourceType: SourceType, sourceId: string, revision: string): Promise<LedgerRow[]> {
    const placeholders = LIVE_STATUSES.map((_, index) => `$${index + 5}`).join(", ");
    const rows = await this.db.query(
      `SELECT * FROM ${LEDGER_TABLE} WHERE company_id = $1 AND source_type = $2 AND source_id = $3 AND revision <> $4 AND status IN (${placeholders})`,
      [companyId, sourceType, sourceId, revision, ...LIVE_STATUSES],
    );
    return rows.map(toRow);
  }

  async update(id: string, patch: LedgerPatch): Promise<LedgerRow | undefined> {
    const sets: string[] = [];
    const params: unknown[] = [];
    const columns: Array<[keyof LedgerPatch, string]> = [["status", "status"], ["datasetId", "dataset_id"], ["dataId", "data_id"], ["error", "error"], ["content", "content"], ["lastCheckedAt", "last_checked_at"]];
    for (const [key, column] of columns) {
      if (key in patch) {
        params.push(patch[key] ?? null);
        sets.push(`${column} = $${params.length}`);
      }
    }
    if (patch.bumpAttempts) sets.push("attempts = attempts + 1");
    params.push(this.clock().toISOString());
    sets.push(`updated_at = $${params.length}`);
    params.push(id);
    await this.db.execute(`UPDATE ${LEDGER_TABLE} SET ${sets.join(", ")} WHERE id = $${params.length}`, params);
    return this.get(id);
  }

  async list(filter: LedgerFilter = {}): Promise<LedgerRow[]> {
    const where: string[] = [];
    const params: unknown[] = [];
    if (filter.companyId) {
      params.push(filter.companyId);
      where.push(`company_id = $${params.length}`);
    }
    if (filter.projectId) {
      params.push(filter.projectId);
      where.push(`project_id = $${params.length}`);
    }
    if (filter.dataset) {
      params.push(filter.dataset);
      where.push(`dataset = $${params.length}`);
    }
    const statuses = filter.status === undefined ? [] : Array.isArray(filter.status) ? filter.status : [filter.status];
    if (statuses.length) {
      const placeholders = statuses.map((status) => {
        params.push(status);
        return `$${params.length}`;
      });
      where.push(`status IN (${placeholders.join(", ")})`);
    }
    params.push(Math.max(1, Math.min(filter.limit ?? 500, 5000)));
    const sql = `SELECT * FROM ${LEDGER_TABLE}${where.length ? ` WHERE ${where.join(" AND ")}` : ""} ORDER BY created_at ASC LIMIT $${params.length}`;
    const rows = await this.db.query(sql, params);
    return rows.map(toRow);
  }

  async countByStatus(companyId?: string): Promise<Record<string, number>> {
    const rows = companyId
      ? await this.db.query<{ status: string; n: unknown }>(`SELECT status, COUNT(*) AS n FROM ${LEDGER_TABLE} WHERE company_id = $1 GROUP BY status`, [companyId])
      : await this.db.query<{ status: string; n: unknown }>(`SELECT status, COUNT(*) AS n FROM ${LEDGER_TABLE} GROUP BY status`);
    const counts: Record<string, number> = {};
    for (const row of rows) counts[String(row.status)] = Number(row.n);
    return counts;
  }
}
