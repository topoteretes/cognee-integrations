// ---------------------------------------------------------------------------
// Dataset switching: move ONE conversation to another Cognee dataset
//
// Parity with the claude-code/codex `cognee-switch-datasets` skill, shaped for
// OpenClaw where a single gateway serves many conversations per agent: the
// switch is scoped to the conversation that asked (keyed by the host's stable
// `sessionKey`, falling back to `sessionId`), never to the whole agent.
//
// A Cognee session never spans two datasets, so a switch is:
//   1. sync the current session into its current dataset (strict; `force`
//      skips a failed sync),
//   2. ensure the target dataset exists and cache its id,
//   3. record an override for this conversation: the target dataset plus a
//      minted session suffix (`open_claw_<id>__2`, `__3`, …) so the entries
//      captured after the switch form a fresh session on the new dataset.
//
// Every later capture write, session-layer recall, graph recall of the
// agent/single scope, and the session-end improve consult the override.
// company/user scopes (multi-scope mode) are shared and stay untouched, and
// memory-file sync keeps following the workspace→dataset routing — the
// switch moves the conversation's memory, not the agent's files.
//
// Overrides persist across gateway restarts and are dropped with
// `action: "reset"`.
// ---------------------------------------------------------------------------

import type { CogneePluginConfig, DatasetOverride, DatasetOverridesFile } from "./types.js";
import { DATASET_OVERRIDES_PATH, loadDatasetOverrides, saveDatasetOverrides } from "./persistence.js";
import { jsonResult, type MemoryTool, type MemoryToolContext } from "./tools.js";

export { loadDatasetOverrides, saveDatasetOverrides };
export type { DatasetOverride, DatasetOverridesFile };

export function datasetOverridesPath(): string {
  return DATASET_OVERRIDES_PATH;
}

/** Conversation keys a hook context may carry, most specific first. */
export function conversationKeys(ctx: { sessionKey?: string; sessionId?: string } | undefined): string[] {
  const keys: string[] = [];
  if (ctx?.sessionKey) keys.push(`key:${ctx.sessionKey}`);
  if (ctx?.sessionId) keys.push(`sid:${ctx.sessionId}`);
  return keys;
}

/** Cognee dataset names: letters, digits, `-` `_` `.`, 1–128 chars. */
export function isValidDatasetName(name: string): boolean {
  return /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(name);
}

export function withSessionSuffix(baseSessionId: string, override: DatasetOverride | undefined): string {
  if (!baseSessionId || !override) return baseSessionId;
  return `${baseSessionId}__${override.sessionSuffix}`;
}

/**
 * In-memory view of the overrides with lazy load and serialized saves. Reads
 * are synchronous so the hot path (capture, recall) never waits on disk; the
 * plugin awaits `ready()` once per hook before consulting it.
 */
export class DatasetSwitchStore {
  private data: DatasetOverridesFile = {};
  private readonly loaded: Promise<void>;
  private chain: Promise<void> = Promise.resolve();

  constructor(private readonly opts: { path?: string; now?: () => Date; warn?: (m: string) => void; debug?: (m: string) => void } = {}) {
    this.loaded = Promise.resolve()
      .then(() => loadDatasetOverrides(opts.path))
      .then((d) => { this.data = { ...d, ...this.data }; })
      .catch((e) => this.opts.debug?.(`cognee-openclaw: dataset overrides load skipped: ${String(e)}`));
  }

  ready(): Promise<void> {
    return this.loaded;
  }

  flush(): Promise<void> {
    return this.chain;
  }

  /** Active override for a conversation, if any. */
  get(ctx: { sessionKey?: string; sessionId?: string } | undefined): DatasetOverride | undefined {
    for (const k of conversationKeys(ctx)) {
      const o = this.data[k];
      if (o) return o;
    }
    return undefined;
  }

  /** Record a switch; returns the new override. */
  set(ctx: { sessionKey?: string; sessionId?: string }, dataset: string, currentDataset: string): DatasetOverride {
    const existing = this.get(ctx);
    const previous = [...(existing?.previous ?? []), currentDataset];
    const override: DatasetOverride = {
      dataset,
      sessionSuffix: (existing?.sessionSuffix ?? 1) + 1,
      switchedAt: (this.opts.now ?? (() => new Date()))().toISOString(),
      previous,
    };
    for (const k of conversationKeys(ctx)) this.data[k] = override;
    this.persist();
    return override;
  }

  /** Drop a conversation's override (back to the configured dataset). */
  clear(ctx: { sessionKey?: string; sessionId?: string }): boolean {
    const override = this.get(ctx);
    if (!override) return false;
    // Drop every alias pointing at this override (a switch is stored under
    // both the sessionKey and the sessionId), plus the keys asked for.
    for (const [k, v] of Object.entries(this.data)) if (v === override) delete this.data[k];
    for (const k of conversationKeys(ctx)) delete this.data[k];
    this.persist();
    return true;
  }

  private persist(): void {
    this.chain = this.chain
      .then(() => this.loaded)
      .then(() => saveDatasetOverrides(this.data, this.opts.path))
      .catch((e) => this.opts.warn?.(`cognee-openclaw: dataset overrides save failed: ${String(e)}`));
  }
}

// ---------------------------------------------------------------------------
// Tool: memory_switch_dataset
// ---------------------------------------------------------------------------

export const MemorySwitchDatasetSchema = {
  type: "object",
  properties: {
    action: {
      type: "string",
      enum: ["list", "current", "switch", "reset"],
      description: "list = datasets available on the server; current = the dataset this conversation writes to and recalls from; switch = move this conversation to `dataset` (created if missing); reset = return to the configured dataset.",
    },
    dataset: { type: "string", description: "switch only: target dataset name (letters, digits, - _ .)." },
    force: {
      type: "boolean",
      description: "switch only: proceed even if syncing the current session into its dataset fails (unsynced turns are retried at session end). Ask the user before setting this.",
    },
  },
  required: ["action"],
  additionalProperties: false,
} as const;

export type MemorySwitchDatasetParams = {
  action: "list" | "current" | "switch" | "reset";
  dataset?: string;
  force?: boolean;
};

export type MemorySwitchDatasetResult =
  | { action: "list"; current: string; datasets: Array<{ name: string; id: string; current: boolean }>; note?: string; error?: string }
  | { action: "current"; dataset: string; sessionId?: string; switched: boolean; previous: string[]; note?: string }
  | {
      action: "switch";
      switched: boolean;
      dataset: string;
      sessionId?: string;
      previous?: { dataset: string; sessionId?: string; synced: boolean };
      reason?: string;
      error?: string;
      note?: string;
    }
  | { action: "reset"; reset: boolean; dataset: string; note?: string };

export type DatasetSwitchDeps = {
  cfg: Required<CogneePluginConfig>;
  store: DatasetSwitchStore;
  /** Dataset this conversation writes to right now (override-aware). */
  currentDataset: (ctx: MemoryToolContext) => string;
  /** Cognee session id for this conversation right now (override-aware). */
  currentSessionId: (ctx: MemoryToolContext) => string | undefined;
  listDatasets: () => Promise<Array<{ id: string; name: string }>>;
  ensureDataset: (name: string) => Promise<string | undefined>;
  /** Bridge the current session into its dataset (server-side improve). */
  syncSession: (datasetName: string, cogneeSessionId: string) => Promise<void>;
  /** Cache a dataset name → id so recall can find it without a server round trip. */
  rememberDatasetId?: (name: string, id: string) => Promise<void>;
  logger?: { debug?: (m: string) => void; warn?: (m: string) => void };
};

export function createDatasetSwitchTool(deps: DatasetSwitchDeps, ctx: MemoryToolContext): MemoryTool<MemorySwitchDatasetParams, MemorySwitchDatasetResult> {
  const convo = { sessionKey: ctx.sessionKey, sessionId: ctx.sessionId };

  async function list(): Promise<MemorySwitchDatasetResult> {
    const current = deps.currentDataset(ctx);
    try {
      const rows = await deps.listDatasets();
      const datasets = rows
        .filter((r) => r && typeof r.name === "string" && r.name)
        .map((r) => ({ name: r.name, id: r.id, current: r.name === current }))
        .sort((a, b) => Number(b.current) - Number(a.current) || a.name.localeCompare(b.name));
      return {
        action: "list",
        current,
        datasets,
        note: "Only datasets this principal can read are listed; a name that is not listed is created on switch. Present the choice to the user and switch only to the one they pick.",
      };
    } catch (e) {
      return { action: "list", current, datasets: [], error: `GET /datasets failed: ${String(e)}` };
    }
  }

  function current(): MemorySwitchDatasetResult {
    const o = deps.store.get(convo);
    return {
      action: "current",
      dataset: deps.currentDataset(ctx),
      ...(deps.currentSessionId(ctx) ? { sessionId: deps.currentSessionId(ctx) } : {}),
      switched: !!o,
      previous: o?.previous ?? [],
      ...(o ? { note: "This conversation was switched away from its configured dataset; action=reset returns to it." } : {}),
    };
  }

  async function doSwitch(params: MemorySwitchDatasetParams): Promise<MemorySwitchDatasetResult> {
    const target = typeof params.dataset === "string" ? params.dataset.trim() : "";
    const before = deps.currentDataset(ctx);
    if (!target) return { action: "switch", switched: false, dataset: before, error: "dataset is required" };
    if (!isValidDatasetName(target)) {
      return { action: "switch", switched: false, dataset: before, error: `invalid dataset name "${target}" — use letters, digits, '-', '_' or '.' (max 128 chars)` };
    }
    if (target === before) return { action: "switch", switched: false, dataset: before, reason: "already_active" };
    if (!ctx.sessionId && !ctx.sessionKey) {
      return { action: "switch", switched: false, dataset: before, error: "no conversation/session in context — dataset switching is per conversation" };
    }

    // 1. Strict sync of the current session into its current dataset.
    const prevSid = deps.currentSessionId(ctx);
    let synced = false;
    if (prevSid) {
      try {
        await deps.syncSession(before, prevSid);
        synced = true;
      } catch (e) {
        if (!params.force) {
          return {
            action: "switch",
            switched: false,
            dataset: before,
            error: `syncing the current session into "${before}" failed: ${String(e)}. Nothing was changed. Retry, or call again with force=true only if the user accepts that unsynced turns are retried at session end instead.`,
          };
        }
        deps.logger?.warn?.(`cognee-openclaw: switch proceeding without sync (force): ${String(e)}`);
      }
    }

    // 2. Ensure the target exists and cache its id for recall.
    try {
      const id = await deps.ensureDataset(target);
      if (id && deps.rememberDatasetId) await deps.rememberDatasetId(target, id);
    } catch (e) {
      return { action: "switch", switched: false, dataset: before, error: `could not create/resolve dataset "${target}": ${String(e)}. Nothing was changed.` };
    }

    // 3. Record the override; later hooks pick it up.
    deps.store.set(convo, target, before);
    const newSid = deps.currentSessionId(ctx);
    deps.logger?.debug?.(`cognee-openclaw: conversation switched ${before} -> ${target} (session ${newSid ?? "-"})`);
    return {
      action: "switch",
      switched: true,
      dataset: target,
      ...(newSid ? { sessionId: newSid } : {}),
      previous: { dataset: before, ...(prevSid ? { sessionId: prevSid } : {}), synced },
      note: "From now on this conversation saves to and recalls from the new dataset; earlier context from the previous dataset is no longer injected — switch back to see it again. Memory files and shared company/user scopes are unaffected.",
    };
  }

  function reset(): MemorySwitchDatasetResult {
    const removed = deps.store.clear(convo);
    return { action: "reset", reset: removed, dataset: deps.currentDataset(ctx), ...(removed ? {} : { note: "This conversation was already on its configured dataset." }) };
  }

  return {
    name: "memory_switch_dataset",
    label: "Memory Switch Dataset",
    description:
      "Move this conversation to another Cognee dataset, or inspect which one it uses. action=list shows available datasets (present them and let the user pick; unknown names are created); action=switch syncs the current session, then binds this conversation to `dataset` for all later saves and recalls; action=current reports the active dataset; action=reset returns to the configured one. " +
      "Switching is per conversation and does not move memory files or shared company/user memory.",
    parameters: MemorySwitchDatasetSchema as unknown as Record<string, unknown>,
    async execute(_id, params) {
      switch (params?.action) {
        case "list": return jsonResult(await list());
        case "current": return jsonResult(current());
        case "switch": return jsonResult(await doSwitch(params));
        case "reset": return jsonResult(reset());
        default: return jsonResult<MemorySwitchDatasetResult>({ action: "current", dataset: deps.currentDataset(ctx), switched: false, previous: [], note: "action must be list, current, switch or reset" });
      }
    },
  };
}
