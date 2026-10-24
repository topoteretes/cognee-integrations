/**
 * The durable outbox around the ledger: enqueue a source record, send pending
 * rows, retire replaced revisions before their replacement is reported clean,
 * track indexing readiness, and clean up a scope. Everything here runs with a
 * backend resolved per company at call time, so a scheduled job (which carries
 * no company id) can still act on rows the event handlers snapshotted.
 */
import type { PluginConfig } from "./config.js";
import type { CogneeClient } from "./cognee-client.js";
import { CogneeApiError, describeError } from "./errors.js";
import { type Clock, type Ledger, type LedgerRow, type LedgerStatus, PENDING_READINESS } from "./ledger.js";
import type { SourceRecord } from "./records.js";
import type { ScopePolicy } from "./scope.js";

export interface CompanyBackend {
  config: PluginConfig;
  client: CogneeClient;
  scope: ScopePolicy;
}

export type BackendResolver = (companyId: string) => Promise<CompanyBackend>;

export interface OutboxLogger {
  info(message: string, meta?: Record<string, unknown>): void;
  warn(message: string, meta?: Record<string, unknown>): void;
  error(message: string, meta?: Record<string, unknown>): void;
  debug(message: string, meta?: Record<string, unknown>): void;
}

export interface OutboxOptions {
  ledger: Ledger;
  backend: BackendResolver;
  logger: OutboxLogger;
  clock?: Clock;
  /** Does the Paperclip source of a row still exist? Used by reconcile to retire deleted sources. */
  sourceExists?: (row: LedgerRow) => Promise<boolean | undefined>;
}

export interface DrainSummary {
  companies: number;
  sent: number;
  retired: number;
  held: number;
  failed: number;
  conflicts: number;
  errors: string[];
}

export interface ReconcileSummary {
  checked: number;
  ready: number;
  indexing: number;
  resent: number;
  failed: number;
  retiredDeletedSources: number;
  errors: string[];
}

export interface CleanupSummary {
  revoked: number;
  deleted: number;
  verifiedAbsent: number;
  stillPresent: number;
  errors: string[];
}

const MAX_SEND_ATTEMPTS = 5;
const MAX_RETIRE_ATTEMPTS = 3;
const UNCERTAIN_GRACE_MS = 15 * 60 * 1000;
const COMPLETED = "DATASET_PROCESSING_COMPLETED";
const ERRORED = "DATASET_PROCESSING_ERRORED";
const OWNED_PREFIXES = ["issue-", "comment-", "decision-"];

export class Outbox {
  readonly ledger: Ledger;
  private readonly backend: BackendResolver;
  private readonly logger: OutboxLogger;
  private readonly clock: Clock;
  private readonly sourceExists?: (row: LedgerRow) => Promise<boolean | undefined>;
  lastDrain: { at: string; summary: DrainSummary } | null = null;
  lastReconcile: { at: string; summary: ReconcileSummary } | null = null;

  constructor(options: OutboxOptions) {
    this.ledger = options.ledger;
    this.backend = options.backend;
    this.logger = options.logger;
    this.clock = options.clock ?? (() => new Date());
    this.sourceExists = options.sourceExists;
  }

  /** Record a source revision; older live revisions of the same source are marked for retirement. */
  async enqueue(record: SourceRecord, dataset: string): Promise<{ row: LedgerRow; created: boolean; superseded: number }> {
    const { row, created } = await this.ledger.upsert({
      companyId: record.companyId,
      projectId: record.projectId,
      sourceType: record.sourceType,
      sourceId: record.sourceId,
      revision: record.revision,
      dataset,
      uploadName: record.uploadName,
      content: record.text,
      sourceRef: record.sourceRef,
      link: record.link,
    });
    let superseded = 0;
    if (created) {
      for (const previous of await this.ledger.otherRevisions(record.companyId, record.sourceType, record.sourceId, record.revision)) {
        await this.ledger.update(previous.id, { status: "superseded", error: null });
        superseded += 1;
      }
    }
    return { row, created, superseded };
  }

  /** Send every company's pending rows (jobs carry no company id: companies come from the rows). */
  async drainAll(limitPerCompany?: number): Promise<DrainSummary> {
    const rows = await this.ledger.list({ status: ["pending", "superseded"], limit: 5000 });
    const companies = Array.from(new Set(rows.map((row) => row.companyId)));
    const summary: DrainSummary = { companies: companies.length, sent: 0, retired: 0, held: 0, failed: 0, conflicts: 0, errors: [] };
    for (const companyId of companies) {
      const partial = await this.drainCompany(companyId, limitPerCompany);
      summary.sent += partial.sent;
      summary.retired += partial.retired;
      summary.held += partial.held;
      summary.failed += partial.failed;
      summary.conflicts += partial.conflicts;
      summary.errors.push(...partial.errors);
    }
    this.lastDrain = { at: this.clock().toISOString(), summary };
    return summary;
  }

  async drainCompany(companyId: string, limit?: number): Promise<DrainSummary> {
    const summary: DrainSummary = { companies: 1, sent: 0, retired: 0, held: 0, failed: 0, conflicts: 0, errors: [] };
    let backend: CompanyBackend;
    try {
      backend = await this.backend(companyId);
    } catch (error) {
      const description = describeError(error);
      summary.errors.push(`company ${companyId}: ${description}`);
      this.logger.warn("drain skipped: backend not resolvable", { companyId, error: description });
      return summary;
    }
    const batch = limit ?? backend.config.maxDrainBatch;
    if (!backend.scope.isCompanyEnabled(companyId)) {
      // The company was disabled after rows were queued: hold them, never send.
      summary.held += (await this.ledger.list({ companyId, status: "pending", limit: batch })).length;
      return summary;
    }

    // 1. Retire replaced revisions first, so a replacement is never reported clean beside its predecessor.
    for (const row of await this.ledger.list({ companyId, status: "superseded", limit: batch })) {
      const outcome = await this.retire(row, backend);
      if (outcome === "retired") summary.retired += 1;
      else if (outcome === "conflict") summary.conflicts += 1;
      else summary.held += 1;
    }

    // 2. Send pending rows whose predecessors are gone.
    for (const row of await this.ledger.list({ companyId, status: "pending", limit: batch })) {
      const blockers = (await this.ledger.otherRevisions(row.companyId, row.sourceType, row.sourceId, row.revision)).filter((other) => other.status === "superseded" || other.status === "conflict");
      if (blockers.length) {
        summary.held += 1;
        continue;
      }
      if (!backend.scope.isProjectEnabled(row.companyId, row.projectId)) {
        await this.ledger.update(row.id, { status: "revoked", content: null, error: "project no longer enabled when the write was due" });
        summary.held += 1;
        continue;
      }
      const outcome = await this.send(row, backend);
      if (outcome === "accepted") summary.sent += 1;
      else if (outcome === "conflict") summary.conflicts += 1;
      else if (outcome === "failed") summary.failed += 1;
      else summary.held += 1;
    }
    return summary;
  }

  private async send(row: LedgerRow, backend: CompanyBackend): Promise<"accepted" | "uncertain" | "failed" | "conflict" | "retry"> {
    if (!row.content) {
      await this.ledger.update(row.id, { status: "failed", error: "no content snapshot to send" });
      return "failed";
    }
    try {
      const response = await backend.client.remember({ datasetName: row.dataset, filename: `${row.uploadName}.txt`, text: row.content }, { timeoutMs: backend.config.writeTimeoutMs });
      const datasetId = typeof response.dataset_id === "string" ? response.dataset_id : null;
      await this.ledger.update(row.id, { status: "accepted", datasetId, error: null, bumpAttempts: true });
      this.logger.info("memory write accepted", { rowId: row.id, companyId: row.companyId, sourceType: row.sourceType, dataset: row.dataset });
      return "accepted";
    } catch (error) {
      const description = describeError(error);
      if (error instanceof CogneeApiError) {
        if (error.outcomeUncertain) {
          await this.ledger.update(row.id, { status: "uncertain", error: description, bumpAttempts: true });
          this.logger.warn("memory write outcome uncertain", { rowId: row.id, error: description });
          return "uncertain";
        }
        if (error.isConflict) {
          await this.ledger.update(row.id, { status: "conflict", error: description, bumpAttempts: true });
          this.logger.warn("memory write conflict; operator reconciliation needed", { rowId: row.id, error: description });
          return "conflict";
        }
        if (error.isRateLimited || error.isServerError) {
          const attempts = row.attempts + 1;
          if (attempts >= MAX_SEND_ATTEMPTS) {
            await this.ledger.update(row.id, { status: "failed", error: `gave up after ${attempts} attempts: ${description}`, bumpAttempts: true });
            return "failed";
          }
          await this.ledger.update(row.id, { error: description, bumpAttempts: true });
          return "retry";
        }
      }
      await this.ledger.update(row.id, { status: "failed", error: description, bumpAttempts: true });
      this.logger.warn("memory write failed", { rowId: row.id, error: description });
      return "failed";
    }
  }

  /**
   * Delete one row's document on the backend and verify by re-listing (the
   * forget endpoint reports success even for unknown ids). A row that was
   * never accepted and has no remote document is retired directly.
   */
  private async retire(row: LedgerRow, backend: CompanyBackend): Promise<"retired" | "held" | "conflict"> {
    const opts = { timeoutMs: backend.config.writeTimeoutMs };
    try {
      let datasetId = row.datasetId;
      if (!datasetId) {
        const dataset = (await backend.client.listDatasets(opts)).find((entry) => entry.name === row.dataset);
        datasetId = dataset?.id ?? null;
      }
      if (!datasetId) {
        // The dataset does not exist: nothing of this row can be on the backend.
        await this.ledger.update(row.id, { status: "retired", content: null, error: null });
        return "retired";
      }
      let dataId = row.dataId;
      if (!dataId) dataId = (await backend.client.findDataItemByName(datasetId, row.uploadName, opts))?.id ?? null;
      if (!dataId) {
        if (row.attempts + 1 >= MAX_RETIRE_ATTEMPTS || row.status === "pending") {
          await this.ledger.update(row.id, { status: "retired", content: null, error: "no remote document found for this revision" });
          return "retired";
        }
        await this.ledger.update(row.id, { error: "remote document not visible yet; retirement retried later", bumpAttempts: true });
        return "held";
      }
      await backend.client.forgetDocument(datasetId, dataId, opts);
      const still = await backend.client.findDataItemByName(datasetId, row.uploadName, opts);
      if (still && still.id === dataId) {
        await this.ledger.update(row.id, { status: "conflict", datasetId, dataId, error: "forget reported success but the document is still listed", bumpAttempts: true });
        return "conflict";
      }
      await this.ledger.update(row.id, { status: "retired", datasetId, dataId, content: null, error: null });
      this.logger.info("memory document retired", { rowId: row.id, sourceType: row.sourceType });
      return "retired";
    } catch (error) {
      const description = describeError(error);
      if (row.attempts + 1 >= MAX_RETIRE_ATTEMPTS) {
        await this.ledger.update(row.id, { status: "conflict", error: `retirement failed: ${description}`, bumpAttempts: true });
        return "conflict";
      }
      await this.ledger.update(row.id, { error: `retirement failed: ${description}`, bumpAttempts: true });
      return "held";
    }
  }

  /** Readiness of accepted writes; resend uncertain ones whose document never appeared; retire deleted sources. */
  async reconcileAll(limit = 200): Promise<ReconcileSummary> {
    const summary: ReconcileSummary = { checked: 0, ready: 0, indexing: 0, resent: 0, failed: 0, retiredDeletedSources: 0, errors: [] };
    const rows = await this.ledger.list({ status: [...PENDING_READINESS], limit });
    const backends = new Map<string, CompanyBackend | null>();
    const resolve = async (companyId: string): Promise<CompanyBackend | null> => {
      if (!backends.has(companyId)) {
        try {
          backends.set(companyId, await this.backend(companyId));
        } catch (error) {
          backends.set(companyId, null);
          summary.errors.push(`company ${companyId}: ${describeError(error)}`);
        }
      }
      return backends.get(companyId) ?? null;
    };
    for (const row of rows) {
      const backend = await resolve(row.companyId);
      if (!backend) continue;
      summary.checked += 1;
      const status = await this.refresh(row, backend);
      if (status === "ready") summary.ready += 1;
      else if (status === "indexing") summary.indexing += 1;
      else if (status === "pending") summary.resent += 1;
      else if (status === "failed") summary.failed += 1;
    }
    if (this.sourceExists) {
      for (const row of await this.ledger.list({ status: "ready", limit })) {
        if (row.sourceType === "decision") continue;
        let exists: boolean | undefined;
        try {
          exists = await this.sourceExists(row);
        } catch (error) {
          summary.errors.push(`source check ${row.id}: ${describeError(error)}`);
          continue;
        }
        if (exists === false) {
          const backend = await resolve(row.companyId);
          if (!backend) continue;
          await this.ledger.update(row.id, { status: "superseded", error: "source deleted in Paperclip" });
          if ((await this.retire(row, backend)) === "retired") summary.retiredDeletedSources += 1;
        }
      }
    }
    this.lastReconcile = { at: this.clock().toISOString(), summary };
    return summary;
  }

  async refresh(row: LedgerRow, backend: CompanyBackend): Promise<LedgerStatus> {
    const checkedAt = this.clock().toISOString();
    const opts = { timeoutMs: backend.config.recallTimeoutMs };
    try {
      let datasetId = row.datasetId;
      if (!datasetId) datasetId = (await backend.client.listDatasets(opts)).find((entry) => entry.name === row.dataset)?.id ?? null;
      // No dataset on the backend means the write never landed: same handling as a
      // document that is not listed (below), not an early return.
      const pipeline = datasetId ? await backend.client.datasetStatus(datasetId, opts) : "";
      const dataId = datasetId ? (row.dataId ?? (await backend.client.findDataItemByName(datasetId, row.uploadName, opts))?.id ?? null) : null;
      let status: LedgerStatus;
      let error: string | null = null;
      if (dataId && pipeline === COMPLETED) status = "ready";
      else if (dataId && pipeline === ERRORED) {
        status = "failed";
        error = "graph pipeline reported an error for this dataset";
      } else if (dataId) status = "indexing";
      else {
        const ageMs = this.clock().getTime() - new Date(row.updatedAt).getTime();
        if (ageMs > UNCERTAIN_GRACE_MS) {
          // The document never appeared: the write did not land. The upload name is
          // content-derived, so resending the same content is safe (identical content
          // is a server-side no-op) — requeue instead of guessing.
          if (row.attempts >= MAX_SEND_ATTEMPTS) {
            status = "failed";
            error = "document never appeared after repeated sends";
          } else {
            status = "pending";
            error = "document not found after the grace period; queued to resend";
          }
        } else status = row.status === "uncertain" ? "uncertain" : "accepted";
      }
      await this.ledger.update(row.id, { status, datasetId, dataId, error, lastCheckedAt: checkedAt });
      return status;
    } catch (error) {
      await this.ledger.update(row.id, { lastCheckedAt: checkedAt, error: `last check failed: ${describeError(error)}` });
      return row.status;
    }
  }

  /**
   * Operator cleanup of one project scope: revoke queued writes first (so a
   * later drain cannot restore removed data), then delete every plugin-owned
   * document in that project's dataset and verify by re-listing.
   */
  async cleanupScope(companyId: string, projectId: string): Promise<CleanupSummary> {
    const summary: CleanupSummary = { revoked: 0, deleted: 0, verifiedAbsent: 0, stillPresent: 0, errors: [] };
    for (const row of await this.ledger.list({ companyId, projectId, status: ["pending", "superseded", "uncertain", "conflict"], limit: 5000 })) {
      await this.ledger.update(row.id, { status: "revoked", content: null, error: "revoked by cleanup" });
      summary.revoked += 1;
    }
    let backend: CompanyBackend;
    try {
      backend = await this.backend(companyId);
    } catch (error) {
      summary.errors.push(describeError(error));
      return summary;
    }
    const dataset = backend.scope.projectDataset(companyId, projectId);
    const opts = { timeoutMs: backend.config.writeTimeoutMs };
    try {
      const datasetId = (await backend.client.listDatasets(opts)).find((entry) => entry.name === dataset)?.id;
      if (!datasetId) return summary;
      const owned = (await backend.client.listDataItemsByPrefix(datasetId, "", opts)).filter((item) => OWNED_PREFIXES.some((prefix) => item.name.startsWith(prefix)));
      for (const item of owned) {
        try {
          await backend.client.forgetDocument(datasetId, item.id, opts);
          summary.deleted += 1;
        } catch (error) {
          summary.errors.push(`forget ${item.name}: ${describeError(error)}`);
        }
      }
      const remaining = new Set((await backend.client.listDataItemsByPrefix(datasetId, "", opts)).map((item) => item.id));
      for (const item of owned) {
        if (remaining.has(item.id)) summary.stillPresent += 1;
        else summary.verifiedAbsent += 1;
      }
      for (const row of await this.ledger.list({ companyId, projectId, status: ["accepted", "indexing", "ready"], limit: 5000 })) {
        const gone = !row.dataId || !remaining.has(row.dataId);
        await this.ledger.update(row.id, gone ? { status: "retired", content: null, error: null } : { status: "conflict", error: "cleanup could not remove this document" });
      }
    } catch (error) {
      summary.errors.push(describeError(error));
    }
    return summary;
  }
}
