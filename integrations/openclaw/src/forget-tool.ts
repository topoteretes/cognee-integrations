// ---------------------------------------------------------------------------
// Agent tool: memory_forget — user-directed deletion of individual documents
//
// "Forget what we talked about tennis" is a judgement call the model has to
// make by reading what is actually stored, so the tool is two-phase and the
// model stays in the loop:
//
//   action=find    list candidate documents in the agent's datasets, with a
//                  preview of each one's raw text and — when `query` is given —
//                  which query terms matched. Nothing is deleted.
//   action=forget  delete exactly the `dataIds` listed, one POST /forget per
//                  document, and only when confirm=true. Refuses anything
//                  broader: whole-dataset and everything-wipes are CLI-only
//                  (`openclaw cognee forget --dataset …`).
//
// Deleting a document removes its raw data, the derived graph knowledge, and
// (cognee >= 1.5.3) the session turns contaminated by it — the Q&A entries
// whose answers cited the deleted graph elements. That invalidation is
// targeted, not whole-session, and tool-call traces are not matched; the
// result text says so, so the model can report honestly.
//
// Content that only exists in the live session cache is not a document yet
// and cannot be listed here; `syncSession=true` on `find` bridges the current
// session into the graph first (server-side improve) so it becomes findable.
// ---------------------------------------------------------------------------

import type { CogneeDataItem, CogneePluginConfig } from "./types.js";
import { jsonResult, type MemoryTool, type MemoryToolContext } from "./tools.js";

export const MemoryForgetSchema = {
  type: "object",
  properties: {
    action: {
      type: "string",
      enum: ["find", "forget"],
      description: "find = list candidate documents (read-only); forget = delete the listed dataIds (requires confirm=true).",
    },
    query: {
      type: "string",
      description: "find only: topic the user wants forgotten. Candidates are ranked by how many query terms their stored text contains; judge matches by meaning, not just by these terms.",
    },
    dataIds: {
      type: "array",
      items: { type: "string" },
      description: "forget only: document ids (from a previous find) to delete. Each is deleted individually.",
    },
    confirm: {
      type: "boolean",
      description: "forget only: must be true. Set it only after the user has confirmed the specific documents to delete.",
    },
    maxCandidates: { type: "integer", minimum: 1, maximum: 50, description: "find only: cap on returned candidates (default 20)." },
    syncSession: {
      type: "boolean",
      description: "find only: bridge the current session into the graph first so this conversation's content is findable and deletable (slower; default false).",
    },
  },
  required: ["action"],
  additionalProperties: false,
} as const;

export type MemoryForgetParams = {
  action: "find" | "forget";
  query?: string;
  dataIds?: string[];
  confirm?: boolean;
  maxCandidates?: number;
  syncSession?: boolean;
};

export type ForgetCandidate = {
  dataId: string;
  datasetId: string;
  /** Scope label (agent/user/company) or dataset name. */
  dataset: string;
  name: string;
  createdAt?: string;
  /** Session the document came from, when recoverable from metadata or the raw header. */
  sessionId?: string;
  preview: string;
  /** Query terms found in the raw text (empty when no query was given). */
  matchedTerms: string[];
};

export type MemoryForgetFindResult = {
  action: "find";
  query?: string;
  candidates: ForgetCandidate[];
  totalDocuments: number;
  scannedDocuments: number;
  sessionSynced?: boolean;
  note: string;
  disabled?: true;
  error?: string;
};

export type MemoryForgetDeleteResult = {
  action: "forget";
  deleted: Array<{ dataId: string; datasetId: string; name?: string }>;
  failed: Array<{ dataId: string; datasetId?: string; error: string }>;
  requiresConfirmation?: true;
  note: string;
  error?: string;
};

export type MemoryForgetResult = MemoryForgetFindResult | MemoryForgetDeleteResult;

export type MemoryForgetDeps = {
  cfg: Required<CogneePluginConfig>;
  resolveDatasets: (agentId?: string, ctx?: MemoryToolContext) => Promise<Array<{ id: string; label: string }>>;
  listDatasetData: (datasetId: string) => Promise<CogneeDataItem[]>;
  readRawData: (datasetId: string, dataId: string, maxChars?: number) => Promise<string>;
  forget: (params: { datasetId: string; dataId: string }) => Promise<{ deleted: boolean; error?: string }>;
  /** Bridge the current session into the graph (improve with session_ids). Optional. */
  syncSession?: (hostSessionId: string, agentId?: string) => Promise<void>;
  logger?: { debug?: (m: string) => void; warn?: (m: string) => void };
};

export const DEFAULT_MAX_CANDIDATES = 20;
/** Raw-text scan is bounded so `find` on a large dataset stays cheap. */
export const MAX_SCANNED_DOCUMENTS = 60;
export const RAW_SCAN_CHARS = 20_000;
/** Enough for the "Session ID:" header plus the preview when no query is being matched. */
export const RAW_PREVIEW_CHARS = 2_000;
export const PREVIEW_CHARS = 300;

const INVALIDATION_NOTE =
  "Deleting a document removes its raw data and derived graph knowledge, and clears the session turns whose answers cited it (targeted, not whole-session; tool-call traces are not matched). Content already in this conversation's context stays visible until the conversation ends.";

/** Lower-cased query terms of >= 3 chars, deduplicated, in order. */
export function queryTerms(query: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of query.toLowerCase().split(/[^\p{L}\p{N}]+/u)) {
    const t = raw.trim();
    if (t.length < 3 || seen.has(t)) continue;
    seen.add(t);
    out.push(t);
  }
  return out;
}

/** "Session ID: xyz" header written by the session→graph bridge, or metadata. */
export function extractSessionId(raw: string, meta?: Record<string, unknown>): string | undefined {
  for (const key of ["session_id", "sessionId"]) {
    const v = meta?.[key];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  const m = /^\s*Session ID:\s*(\S+)/m.exec(raw.slice(0, 2_000));
  return m?.[1];
}

export function preview(raw: string): string {
  const flat = raw.replace(/\s+/g, " ").trim();
  return flat.length > PREVIEW_CHARS ? `${flat.slice(0, PREVIEW_CHARS)}…` : flat;
}

export function createMemoryForgetTool(deps: MemoryForgetDeps, ctx: MemoryToolContext): MemoryTool<MemoryForgetParams, MemoryForgetResult> {
  // dataId -> datasetId from the last find, so forget needn't re-list.
  const located = new Map<string, { datasetId: string; name: string }>();

  async function find(params: MemoryForgetParams): Promise<MemoryForgetFindResult> {
    const query = typeof params.query === "string" ? params.query.trim() : "";
    const terms = query ? queryTerms(query) : [];
    const cap = typeof params.maxCandidates === "number" && params.maxCandidates >= 1 ? Math.min(50, Math.floor(params.maxCandidates)) : DEFAULT_MAX_CANDIDATES;

    let sessionSynced: boolean | undefined;
    if (params.syncSession && ctx.sessionId && deps.syncSession) {
      try {
        await deps.syncSession(ctx.sessionId, ctx.agentId);
        sessionSynced = true;
      } catch (e) {
        deps.logger?.warn?.(`cognee-openclaw: memory_forget session sync failed: ${String(e)}`);
        sessionSynced = false;
      }
    }

    let datasets: Array<{ id: string; label: string }>;
    try {
      datasets = await deps.resolveDatasets(ctx.agentId, ctx);
    } catch (e) {
      return { action: "find", ...(query ? { query } : {}), candidates: [], totalDocuments: 0, scannedDocuments: 0, disabled: true, error: `dataset resolution failed: ${String(e)}`, note: "Cognee is unreachable; retry later." };
    }
    if (datasets.length === 0) {
      return { action: "find", ...(query ? { query } : {}), candidates: [], totalDocuments: 0, scannedDocuments: 0, note: "No Cognee dataset is indexed for this agent yet — nothing to forget." };
    }

    const items: Array<CogneeDataItem & { label: string }> = [];
    const listErrors: string[] = [];
    for (const ds of datasets) {
      try {
        const list = await deps.listDatasetData(ds.id);
        for (const it of list) items.push({ ...it, datasetId: it.datasetId || ds.id, label: ds.label });
      } catch (e) {
        listErrors.push(`${ds.label}: ${String(e)}`);
      }
    }
    if (items.length === 0 && listErrors.length === datasets.length) {
      return { action: "find", ...(query ? { query } : {}), candidates: [], totalDocuments: 0, scannedDocuments: 0, disabled: true, error: listErrors[0], note: "Cognee is unreachable; retry later." };
    }

    // Newest first so a bounded scan covers the recent sessions the user most
    // likely means.
    items.sort((a, b) => (b.createdAt ?? "").localeCompare(a.createdAt ?? ""));
    const toScan = items.slice(0, MAX_SCANNED_DOCUMENTS);

    const candidates: ForgetCandidate[] = [];
    for (const it of toScan) {
      let raw = "";
      try {
        // The server returns the whole document either way; the cap only bounds
        // what is kept in memory — 20K when there are terms to match, 2K for
        // header + preview otherwise.
        raw = await deps.readRawData(it.datasetId, it.id, terms.length > 0 ? RAW_SCAN_CHARS : RAW_PREVIEW_CHARS);
      } catch (e) {
        deps.logger?.debug?.(`cognee-openclaw: memory_forget raw read failed for ${it.id}: ${String(e)}`);
      }
      const lower = raw.toLowerCase();
      const matched = terms.filter((t) => lower.includes(t));
      if (terms.length > 0 && matched.length === 0) continue;
      const sessionId = extractSessionId(raw, it.externalMetadata);
      candidates.push({
        dataId: it.id,
        datasetId: it.datasetId,
        dataset: it.label,
        name: it.name,
        ...(it.createdAt ? { createdAt: it.createdAt } : {}),
        ...(sessionId ? { sessionId } : {}),
        preview: preview(raw) || "(no raw text available)",
        matchedTerms: matched,
      });
    }
    candidates.sort((a, b) => b.matchedTerms.length - a.matchedTerms.length || (b.createdAt ?? "").localeCompare(a.createdAt ?? ""));
    const out = candidates.slice(0, cap);
    for (const c of out) located.set(c.dataId, { datasetId: c.datasetId, name: c.name });

    const notes: string[] = [];
    notes.push(
      out.length === 0
        ? (terms.length > 0 ? "No stored document mentions those terms. Do not delete 'closest' documents on a miss; tell the user nothing matched." : "No documents found.")
        : "Read the previews and judge by meaning. Group documents from the same session (same sessionId) — deleting one while keeping its siblings leaves the topic recallable. Show the user the list and get confirmation, then call action=forget with confirm=true.",
    );
    if (items.length > toScan.length) notes.push(`Only the ${toScan.length} most recent of ${items.length} documents were scanned; narrow the query or ask the user for a timeframe if the topic may be older.`);
    if (listErrors.length > 0) notes.push(`Some datasets could not be listed: ${listErrors.join("; ").slice(0, 300)}`);
    if (sessionSynced === false) notes.push("The current session could not be synced first; content from this conversation may not appear.");
    if (!params.syncSession && ctx.sessionId) notes.push("Content from the current conversation is not a document yet; pass syncSession=true to include it.");

    return {
      action: "find",
      ...(query ? { query } : {}),
      candidates: out,
      totalDocuments: items.length,
      scannedDocuments: toScan.length,
      ...(sessionSynced !== undefined ? { sessionSynced } : {}),
      note: notes.join(" "),
    };
  }

  async function forget(params: MemoryForgetParams): Promise<MemoryForgetDeleteResult> {
    const ids = Array.isArray(params.dataIds) ? params.dataIds.map(String).map((s) => s.trim()).filter(Boolean) : [];
    if (ids.length === 0) {
      return { action: "forget", deleted: [], failed: [], error: "dataIds is required — run action=find first and pass the ids the user confirmed.", note: INVALIDATION_NOTE };
    }
    if (params.confirm !== true) {
      return {
        action: "forget",
        deleted: [],
        failed: [],
        requiresConfirmation: true,
        error: `Nothing deleted. Show the user the ${ids.length} document(s) (id + one-line summary), state that session turns which relied on them are cleared too, and call again with confirm=true once they agree.`,
        note: INVALIDATION_NOTE,
      };
    }

    // Resolve dataset for ids not seen by this instance's find (e.g. the model
    // carried ids over from another turn): list the agent's datasets once.
    const unknown = ids.filter((id) => !located.has(id));
    if (unknown.length > 0) {
      try {
        for (const ds of await deps.resolveDatasets(ctx.agentId, ctx)) {
          const list = await deps.listDatasetData(ds.id);
          for (const it of list) if (unknown.includes(it.id)) located.set(it.id, { datasetId: it.datasetId || ds.id, name: it.name });
        }
      } catch (e) {
        deps.logger?.warn?.(`cognee-openclaw: memory_forget could not resolve datasets: ${String(e)}`);
      }
    }

    const deleted: MemoryForgetDeleteResult["deleted"] = [];
    const failed: MemoryForgetDeleteResult["failed"] = [];
    for (const dataId of ids) {
      const loc = located.get(dataId);
      if (!loc) {
        failed.push({ dataId, error: "unknown data id for this agent's datasets (already deleted, or not from a find in this session)" });
        continue;
      }
      const r = await deps.forget({ datasetId: loc.datasetId, dataId });
      if (r.deleted) {
        deleted.push({ dataId, datasetId: loc.datasetId, name: loc.name });
        located.delete(dataId);
      } else {
        const err = r.error ?? "unknown error";
        // A 404 means it is already gone — report as deleted, not failed.
        if (/\(404\)|not found/i.test(err)) deleted.push({ dataId, datasetId: loc.datasetId, name: loc.name });
        else failed.push({ dataId, datasetId: loc.datasetId, error: err });
      }
    }
    deps.logger?.debug?.(`cognee-openclaw: memory_forget deleted ${deleted.length}/${ids.length} document(s)`);
    return { action: "forget", deleted, failed, note: INVALIDATION_NOTE };
  }

  return {
    name: "memory_forget",
    label: "Memory Forget",
    description:
      "Forget specific things from Cognee memory when the user asks (\"forget what we said about tennis\", \"delete that from memory\"). Two steps: action=find with a query lists candidate documents with previews (read-only) — judge matches by meaning and group same-session siblings; then, after the user confirms the exact documents, action=forget with dataIds and confirm=true deletes them one by one. " +
      "Never use this for clearing a whole dataset or all memory; that is an operator CLI action. Report exactly what was deleted and what was kept.",
    parameters: MemoryForgetSchema as unknown as Record<string, unknown>,
    async execute(_id, params) {
      const action = params?.action;
      if (action === "forget") return jsonResult(await forget(params));
      if (action === "find") return jsonResult(await find(params));
      return jsonResult<MemoryForgetResult>({ action: "find", candidates: [], totalDocuments: 0, scannedDocuments: 0, error: "action must be 'find' or 'forget'", note: "" });
    },
  };
}
