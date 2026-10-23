/**
 * `MemoryService` is the business logic behind the three tools and the
 * operator CLI: fixed scope, bounded inputs, receipt ledger, indexing status,
 * verified deletion. It knows nothing about MCP, so a supported official
 * cognee MCP preset could replace `server.ts`/`tools.ts` without touching it.
 */
import { createHash } from "node:crypto";
import type { LovableMemoryConfig, RecallMode } from "./config.js";
import { CogneeClient, type RecallItem } from "./cognee-client.js";
import { CogneeApiError, describeError } from "./errors.js";
import type { Logger } from "./logger.js";
import { silentLogger } from "./logger.js";
import { PENDING_STATUSES, ReceiptStore, type Receipt, type ReceiptStatus } from "./receipts.js";
import { ScopePolicy } from "./scope.js";

export interface RecallEvidence {
  text: string;
  dataset: string;
  score: number | null;
  documentName: string | null;
  dataId: string | null;
  kind: string;
}

export interface RecallResult {
  status: "ok" | "empty" | "unavailable" | "error";
  mode: RecallMode;
  query: string;
  datasetsSearched: string[];
  /** Raw retrieved passages (mode "evidence"). */
  evidence: RecallEvidence[];
  /** LLM-synthesized answer (mode "answer"); labelled as such, never as evidence. */
  answer?: string;
  truncated: boolean;
  droppedOutOfScope: number;
  message?: string;
}

export interface RememberResult {
  receiptId: string;
  status: ReceiptStatus;
  dataset: string;
  deduplicated: boolean;
  message: string;
  error?: string;
}

export interface StatusResult {
  receiptId: string;
  status: ReceiptStatus | "not_found";
  dataset?: string;
  datasetId?: string | null;
  dataId?: string | null;
  createdAt?: string;
  checkedAt: string;
  message: string;
}

export interface ForgetResult {
  receiptId: string;
  verified: boolean;
  status: ReceiptStatus | "not_found";
  message: string;
}

export interface HealthReport {
  backendReachable: boolean;
  backendStatus: number;
  projectDataset: string;
  projectDatasetId: string | null;
  sharedDatasets: { name: string; available: boolean }[];
  receipts: Record<string, number>;
  receiptsDurable: boolean;
  problems: string[];
}

export interface MemoryServiceOptions {
  config: LovableMemoryConfig;
  client?: CogneeClient;
  store?: ReceiptStore;
  logger?: Logger;
  clock?: () => Date;
}

const COMPLETED = "DATASET_PROCESSING_COMPLETED";
const ERRORED = "DATASET_PROCESSING_ERRORED";
/** After this long without the document appearing, the write is "unknown", not "indexing". */
const INDEXING_GRACE_MS = 15 * 60 * 1000;

export function hashContent(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

export function normalizeText(value: string, maxChars: number): string {
  return value.replace(/\r\n?/g, "\n").trim().slice(0, maxChars);
}

export class MemoryService {
  readonly config: LovableMemoryConfig;
  readonly scope: ScopePolicy;
  readonly client: CogneeClient;
  readonly store: ReceiptStore;
  private readonly logger: Logger;
  private readonly clock: () => Date;
  private projectDatasetId: string | null = null;
  /** Shared datasets confirmed to exist on the backend (missing ones are skipped, never created). */
  private availableShared: Set<string> | null = null;

  constructor(options: MemoryServiceOptions) {
    this.config = options.config;
    this.scope = new ScopePolicy(options.config.projectDataset, options.config.sharedDatasets);
    this.client =
      options.client ??
      new CogneeClient({
        baseUrl: options.config.cognee.baseUrl,
        apiKey: options.config.cognee.apiKey,
        requestTimeoutMs: options.config.cognee.requestTimeoutMs,
        maxResponseBytes: options.config.cognee.maxResponseBytes,
      });
    this.clock = options.clock ?? (() => new Date());
    this.store = options.store ?? new ReceiptStore(options.config.receipts.path, { clock: this.clock });
    this.logger = options.logger ?? silentLogger;
  }

  close(): void {
    this.store.close();
  }

  // -- startup / health -----------------------------------------------------

  /** Verify the backend, create the project dataset, note which shared datasets exist. */
  async prepare(): Promise<HealthReport> {
    const report = await this.health();
    if (!report.backendReachable) return report;
    try {
      const dataset = await this.client.ensureDataset(this.scope.writeDataset, { timeoutMs: this.config.cognee.statusTimeoutMs });
      this.projectDatasetId = dataset.id;
      report.projectDatasetId = dataset.id;
    } catch (error) {
      report.problems.push(`project dataset ${this.scope.writeDataset}: ${describeError(error)}`);
    }
    return report;
  }

  async health(): Promise<HealthReport> {
    const problems: string[] = [];
    let backendReachable = false;
    let backendStatus = 0;
    try {
      const health = await this.client.health({ timeoutMs: this.config.cognee.statusTimeoutMs });
      backendReachable = health.reachable && health.status < 500;
      backendStatus = health.status;
      if (!backendReachable) problems.push(`cognee /health answered HTTP ${health.status}`);
    } catch (error) {
      problems.push(`cognee unreachable: ${describeError(error)}`);
    }
    const shared: HealthReport["sharedDatasets"] = [];
    if (backendReachable) {
      try {
        const names = await this.refreshSharedDatasets();
        for (const name of this.config.sharedDatasets) {
          const available = names.has(name);
          shared.push({ name, available });
          if (!available) problems.push(`shared dataset ${name} does not exist on the backend (it is read-only here and will not be created)`);
        }
        if (this.projectDatasetId === null) {
          const found = (await this.client.listDatasets({ timeoutMs: this.config.cognee.statusTimeoutMs })).find((row) => row.name === this.scope.writeDataset);
          if (found) this.projectDatasetId = found.id;
        }
      } catch (error) {
        problems.push(`dataset listing failed: ${describeError(error)}`);
        for (const name of this.config.sharedDatasets) shared.push({ name, available: false });
      }
    } else {
      for (const name of this.config.sharedDatasets) shared.push({ name, available: false });
    }
    if (!this.store.durable) problems.push("receipt ledger is in-memory: pending writes do not survive a restart");
    return {
      backendReachable,
      backendStatus,
      projectDataset: this.scope.writeDataset,
      projectDatasetId: this.projectDatasetId,
      sharedDatasets: shared,
      receipts: this.store.countByStatus(),
      receiptsDurable: this.store.durable,
      problems,
    };
  }

  private async refreshSharedDatasets(): Promise<Set<string>> {
    const rows = await this.client.listDatasets({ timeoutMs: this.config.cognee.statusTimeoutMs });
    const names = new Set(rows.map((row) => row.name));
    this.availableShared = new Set(this.config.sharedDatasets.filter((name) => names.has(name)));
    const project = rows.find((row) => row.name === this.scope.writeDataset);
    if (project) this.projectDatasetId = project.id;
    return names;
  }

  private readableDatasets(): string[] {
    const shared = this.availableShared ? this.config.sharedDatasets.filter((name) => this.availableShared!.has(name)) : this.config.sharedDatasets;
    return [this.scope.writeDataset, ...shared];
  }

  // -- recall ---------------------------------------------------------------

  async recall(input: { query: string; limit?: number; mode?: RecallMode }): Promise<RecallResult> {
    const limits = this.config.limits;
    const query = normalizeText(input.query ?? "", limits.maxQueryChars);
    const mode: RecallMode = input.mode ?? this.config.recall.defaultMode;
    const topK = Math.max(1, Math.min(input.limit ?? limits.defaultTopK, limits.maxRecallResults));
    const base: RecallResult = { status: "empty", mode, query, datasetsSearched: [], evidence: [], truncated: false, droppedOutOfScope: 0 };
    if (!query) return { ...base, status: "error", message: "query is empty" };
    if (mode === "answer" && !this.config.recall.allowAnswerMode) {
      return { ...base, status: "error", message: "answer mode is disabled on this deployment; use mode \"evidence\"" };
    }

    let datasets = this.readableDatasets();
    base.datasetsSearched = datasets;
    let items: RecallItem[];
    try {
      items = await this.recallWithScope(query, datasets, mode, topK);
    } catch (error) {
      if (error instanceof CogneeApiError && isNoSearchableMemory(error)) {
        // cognee 1.6.5 answers a completion search on a dataset that exists but
        // holds no (remaining) searchable memory with 404 NoDataError.
        return { ...base, status: "empty", message: "nothing searchable has been remembered in this project yet" };
      }
      if (error instanceof CogneeApiError && error.isNotFound) {
        // A dataset named in the request does not exist. The project dataset is
        // created on the first write, so an empty project is "nothing stored
        // yet", not a failure; a missing shared dataset is dropped and retried.
        try {
          await this.refreshSharedDatasets();
        } catch {
          /* fall through to the report below */
        }
        datasets = this.readableDatasets();
        base.datasetsSearched = datasets;
        if (!this.projectDatasetId) {
          return { ...base, status: "empty", message: "nothing has been remembered in this project yet" };
        }
        try {
          items = await this.recallWithScope(query, datasets, mode, topK);
        } catch (retryError) {
          if (retryError instanceof CogneeApiError && isNoSearchableMemory(retryError)) {
            return { ...base, status: "empty", message: "nothing searchable has been remembered in this project yet" };
          }
          return this.recallFailure(base, retryError);
        }
      } else {
        return this.recallFailure(base, error);
      }
    }

    const { kept, dropped } = this.scope.filterReadable(items);
    base.droppedOutOfScope = dropped;
    if (dropped) this.logger.warn("recall returned out-of-scope items; dropped", { dropped });

    if (mode === "answer") {
      const answer = kept.map((item) => (typeof item.text === "string" ? item.text : "")).filter(Boolean).join("\n\n");
      if (!answer.trim()) return { ...base, status: "empty", message: "no relevant product context found" };
      const bounded = answer.slice(0, limits.maxRecallChars);
      return { ...base, status: "ok", answer: bounded, truncated: bounded.length < answer.length };
    }

    const evidence: RecallEvidence[] = [];
    let used = 0;
    let truncated = false;
    const sorted = [...kept].sort((a, b) => (typeof b.score === "number" ? b.score : -1) - (typeof a.score === "number" ? a.score : -1));
    for (const item of sorted) {
      const text = typeof item.text === "string" ? item.text.trim() : "";
      if (!text) continue;
      if (evidence.length >= topK) {
        truncated = true;
        break;
      }
      const remaining = limits.maxRecallChars - used;
      if (remaining <= 0) {
        truncated = true;
        break;
      }
      const clipped = text.length > remaining ? `${text.slice(0, remaining)}…` : text;
      if (clipped.length < text.length) truncated = true;
      used += clipped.length;
      const metadata = (item.metadata ?? {}) as Record<string, unknown>;
      evidence.push({
        text: clipped,
        dataset: String(item.dataset_name),
        score: typeof item.score === "number" ? item.score : null,
        documentName: typeof metadata.document_name === "string" ? metadata.document_name : null,
        dataId: typeof metadata.data_id === "string" ? metadata.data_id : null,
        kind: typeof item.kind === "string" ? item.kind : "chunk",
      });
    }
    if (!evidence.length) return { ...base, status: "empty", message: "no relevant product context found" };
    return { ...base, status: "ok", evidence, truncated };
  }

  private async recallWithScope(query: string, datasets: string[], mode: RecallMode, topK: number): Promise<RecallItem[]> {
    return this.client.recall(
      { query, datasets, searchType: mode === "answer" ? "GRAPH_COMPLETION" : "CHUNKS", topK, includeReferences: mode === "answer" },
      { timeoutMs: this.config.cognee.recallTimeoutMs, retries: 1 },
    );
  }

  private recallFailure(base: RecallResult, error: unknown): RecallResult {
    const description = describeError(error);
    this.logger.warn("recall failed", { error: description });
    if (error instanceof CogneeApiError) {
      if (error.isAuth) return { ...base, status: "unavailable", message: "memory backend rejected the connector's credentials (operator: check COGNEE_API_KEY)" };
      if (error.isRateLimited) return { ...base, status: "unavailable", message: "memory backend is rate limiting; retry shortly" };
      if (error.kind === "timeout") return { ...base, status: "unavailable", message: "memory backend timed out; retry shortly" };
      if (error.kind === "network") return { ...base, status: "unavailable", message: "memory backend unreachable; retry shortly" };
    }
    return { ...base, status: "error", message: `memory lookup failed: ${description}` };
  }

  // -- remember -------------------------------------------------------------

  async rememberDecision(input: { content: string; rationale?: string; sourceReference?: string; requestId?: string; kind?: string }): Promise<RememberResult> {
    const limits = this.config.limits;
    const content = normalizeText(input.content ?? "", limits.maxContentChars);
    const rationale = input.rationale ? normalizeText(input.rationale, limits.maxRationaleChars) : "";
    const sourceRef = input.sourceReference ? normalizeText(input.sourceReference, limits.maxSourceRefChars).replace(/\s+/g, " ") : "";
    const kind = (input.kind ?? "decision").replace(/[^a-z_]/g, "").slice(0, 32) || "decision";
    const dataset = this.scope.writeDataset;
    if (!content) {
      return { receiptId: "", status: "failed", dataset, deduplicated: false, message: "content is empty", error: "content is empty" };
    }

    const body = this.buildDocument({ kind, content, rationale, sourceRef });
    const contentHash = hashContent(body.text);
    const uploadName = `${kind}-${contentHash.slice(0, 32)}`;
    const requestId = (input.requestId ?? "").trim().slice(0, 128) || `content:${contentHash}`;

    const replay = this.store.getByRequestId(requestId);
    if (replay) {
      return { receiptId: replay.id, status: replay.status, dataset, deduplicated: true, message: `request ${requestId} was already recorded as receipt ${replay.id} (${replay.status})` };
    }
    const duplicate = this.store.findLiveByContent(dataset, contentHash);
    if (duplicate) {
      return { receiptId: duplicate.id, status: duplicate.status, dataset, deduplicated: true, message: `identical content is already recorded as receipt ${duplicate.id} (${duplicate.status})` };
    }

    const { receipt } = this.store.create({ requestId, contentHash, uploadName, kind, dataset, content, rationale: rationale || null, sourceRef: sourceRef || null });
    try {
      const response = await this.client.remember({ datasetName: dataset, filename: `${uploadName}.txt`, text: body.text }, { timeoutMs: this.config.cognee.writeTimeoutMs });
      const datasetId = typeof response.dataset_id === "string" ? response.dataset_id : this.projectDatasetId;
      if (datasetId) this.projectDatasetId = datasetId;
      this.store.update(receipt.id, { status: "accepted", datasetId: datasetId ?? null, error: null });
      this.logger.info("decision accepted", { receiptId: receipt.id, dataset, status: response.status });
      return {
        receiptId: receipt.id, status: "accepted", dataset, deduplicated: false,
        message: `accepted by the memory backend; graph indexing runs in the background. Check memory_status with receipt ${receipt.id} before relying on recall.`,
      };
    } catch (error) {
      const description = describeError(error);
      if (error instanceof CogneeApiError && error.outcomeUncertain) {
        // The request may have reached the server. Keep the receipt so a later
        // status check or reconcile can find the document by its upload name.
        this.store.update(receipt.id, { status: "unknown", error: description });
        this.logger.warn("decision write outcome uncertain", { receiptId: receipt.id, error: description });
        return { receiptId: receipt.id, status: "unknown", dataset, deduplicated: false, error: description, message: `the write timed out or lost the connection; its outcome is unknown. Check memory_status with receipt ${receipt.id} later; do not resend blindly.` };
      }
      this.store.update(receipt.id, { status: "failed", error: description });
      this.logger.warn("decision write failed", { receiptId: receipt.id, error: description });
      return { receiptId: receipt.id, status: "failed", dataset, deduplicated: false, error: description, message: `the memory backend rejected the write: ${this.publicError(error)}` };
    }
  }

  private buildDocument(parts: { kind: string; content: string; rationale: string; sourceRef: string }): { text: string } {
    const recordedAt = this.clock().toISOString();
    const lines = [
      `Product ${parts.kind.replace(/_/g, " ")} (Lovable project memory, dataset ${this.scope.writeDataset})`,
      `Recorded: ${recordedAt}`,
      `Source reference: ${parts.sourceRef || "none provided"}`,
      "",
      `${capitalize(parts.kind.replace(/_/g, " "))}:`,
      parts.content,
    ];
    if (parts.rationale) lines.push("", "Rationale:", parts.rationale);
    return { text: `${lines.join("\n")}\n` };
  }

  private publicError(error: unknown): string {
    if (error instanceof CogneeApiError) {
      if (error.isAuth) return "backend rejected the connector's credentials (operator: check COGNEE_API_KEY)";
      if (error.isRateLimited) return "backend is rate limiting; retry later";
      if (error.status === 409) return "backend reported a conflict (HTTP 409); the operator can inspect receipts";
      if (error.isServerError) return `backend error (HTTP ${error.status})`;
      if (error.kind === "http") return `HTTP ${error.status}${error.detail ? `: ${error.detail.slice(0, 160)}` : ""}`;
      return error.kind;
    }
    return "unexpected error";
  }

  // -- status / reconcile ---------------------------------------------------

  async status(receiptId: string): Promise<StatusResult> {
    const checkedAt = this.clock().toISOString();
    const receipt = this.store.get(String(receiptId ?? "").trim());
    if (!receipt) return { receiptId: String(receiptId ?? ""), status: "not_found", checkedAt, message: "no receipt with that id exists on this deployment" };
    const refreshed = PENDING_STATUSES.includes(receipt.status) ? await this.refreshReceipt(receipt) : receipt;
    return {
      receiptId: refreshed.id,
      status: refreshed.status,
      dataset: refreshed.dataset,
      datasetId: refreshed.datasetId,
      dataId: refreshed.dataId,
      createdAt: refreshed.createdAt,
      checkedAt,
      message: this.describeStatus(refreshed),
    };
  }

  /** Re-check every pending receipt against the backend. */
  async reconcile(limit = 200): Promise<{ checked: number; byStatus: Record<string, number> }> {
    const pending = this.store.listPending(limit);
    const byStatus: Record<string, number> = {};
    for (const receipt of pending) {
      const updated = await this.refreshReceipt(receipt);
      byStatus[updated.status] = (byStatus[updated.status] ?? 0) + 1;
    }
    return { checked: pending.length, byStatus };
  }

  private async refreshReceipt(receipt: Receipt): Promise<Receipt> {
    const checkedAt = this.clock().toISOString();
    const opts = { timeoutMs: this.config.cognee.statusTimeoutMs };
    try {
      let datasetId = receipt.datasetId ?? this.projectDatasetId;
      if (!datasetId) {
        const found = (await this.client.listDatasets(opts)).find((row) => row.name === receipt.dataset);
        if (!found) {
          return this.store.update(receipt.id, { lastCheckedAt: checkedAt, bumpChecks: true, error: "dataset does not exist on the backend" }) ?? receipt;
        }
        datasetId = found.id;
        this.projectDatasetId = found.id;
      }
      const pipeline = await this.client.datasetStatus(datasetId, opts);
      let dataId = receipt.dataId;
      if (!dataId) {
        const item = await this.client.findDataItemByName(datasetId, receipt.uploadName, opts);
        dataId = item?.id ?? null;
      }
      let status: ReceiptStatus;
      let error: string | null = null;
      if (dataId && pipeline === COMPLETED) status = "ready";
      else if (dataId && pipeline === ERRORED) {
        status = "failed";
        error = "graph pipeline reported an error for this dataset";
      } else if (dataId) status = "indexing";
      else if (pipeline === ERRORED) {
        status = "failed";
        error = "graph pipeline reported an error before the document was stored";
      } else {
        const ageMs = this.clock().getTime() - new Date(receipt.createdAt).getTime();
        status = ageMs > INDEXING_GRACE_MS ? "unknown" : receipt.status === "unknown" ? "unknown" : "indexing";
        if (status === "unknown") error = receipt.error ?? "document not found on the backend after the grace period";
      }
      return this.store.update(receipt.id, { status, datasetId, dataId, error, lastCheckedAt: checkedAt, bumpChecks: true }) ?? receipt;
    } catch (caught) {
      const description = describeError(caught);
      this.logger.warn("status check failed", { receiptId: receipt.id, error: description });
      return this.store.update(receipt.id, { lastCheckedAt: checkedAt, bumpChecks: true, error: `last check failed: ${description}` }) ?? receipt;
    }
  }

  private describeStatus(receipt: Receipt): string {
    switch (receipt.status) {
      case "accepted":
        return "stored by the backend; graph indexing has not been confirmed yet";
      case "indexing":
        return "document stored; the knowledge graph is still being built, so recall may not see it yet";
      case "ready":
        return "indexed; recall can return this record";
      case "failed":
        return `failed: ${receipt.error ?? "unknown reason"}`;
      case "unknown":
        return `outcome unknown: ${receipt.error ?? "the backend could not confirm this write"}; an operator can run reconcile or resend`;
      case "deleted":
        return "deleted by an operator; it no longer contributes to recall";
      default:
        return receipt.status;
    }
  }

  // -- deletion (operator path) --------------------------------------------

  /**
   * Delete one receipt's document from the backend and verify it is gone by
   * re-listing the dataset (the forget endpoint answers "success" even for an
   * unknown id). Only then is the receipt marked deleted and its content cleared.
   */
  async forget(receiptId: string): Promise<ForgetResult> {
    const receipt = this.store.get(String(receiptId ?? "").trim());
    if (!receipt) return { receiptId: String(receiptId ?? ""), verified: false, status: "not_found", message: "no receipt with that id" };
    if (receipt.status === "deleted") return { receiptId: receipt.id, verified: true, status: "deleted", message: "already deleted" };
    const opts = { timeoutMs: this.config.cognee.statusTimeoutMs };
    // Never let a later reconcile flip a deleted record back: freeze the status first.
    const current = PENDING_STATUSES.includes(receipt.status) ? await this.refreshReceipt(receipt) : receipt;
    if (!current.datasetId) {
      return { receiptId: receipt.id, verified: false, status: current.status, message: "no dataset id is known for this receipt; nothing to delete remotely" };
    }
    if (!current.dataId) {
      if (current.status === "failed") {
        this.store.update(receipt.id, { status: "deleted", content: null, rationale: null, error: null });
        return { receiptId: receipt.id, verified: true, status: "deleted", message: "the write had failed; no remote document existed. Receipt content cleared." };
      }
      return { receiptId: receipt.id, verified: false, status: current.status, message: "the document is not (yet) visible on the backend, so it cannot be deleted; reconcile later" };
    }
    try {
      await this.client.forgetDocument(current.datasetId, current.dataId, opts);
      const stillThere = await this.client.findDataItemByName(current.datasetId, current.uploadName, opts);
      if (stillThere && stillThere.id === current.dataId) {
        this.store.update(receipt.id, { error: "forget reported success but the document is still listed" });
        return { receiptId: receipt.id, verified: false, status: current.status, message: "the backend still lists the document after deletion; not marking it deleted" };
      }
      this.store.update(receipt.id, { status: "deleted", content: null, rationale: null, error: null });
      this.logger.info("decision deleted", { receiptId: receipt.id });
      return { receiptId: receipt.id, verified: true, status: "deleted", message: "deleted on the backend and verified absent from the dataset listing; local content cleared" };
    } catch (error) {
      const description = describeError(error);
      this.store.update(receipt.id, { error: `delete failed: ${description}` });
      return { receiptId: receipt.id, verified: false, status: current.status, message: `delete failed: ${description}` };
    }
  }
}

function isNoSearchableMemory(error: CogneeApiError): boolean {
  return error.isNotFound && /no searchable memory|no data has been added|NoDataError/i.test(error.detail);
}

function capitalize(value: string): string {
  return value ? value[0].toUpperCase() + value.slice(1) : value;
}
