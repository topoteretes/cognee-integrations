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
// Every later capture write, graph recall of the
// agent/single scope, and the session-end improve consult the override.
// company/user scopes (multi-scope mode) are shared and stay untouched, and
// memory-file sync keeps following the workspace→dataset routing — the
// switch moves the conversation's memory, not the agent's files.
//
// Overrides persist across gateway restarts and are dropped with
// `action: "reset"`.
//
// Recall mode (SDK-1393): graph recall is "auto" — it names no dataset and a
// cognee >= 1.6.6 server routes the query (best effort: the best-matching
// dataset when it can, otherwise every readable dataset; an older server
// searches every readable dataset) — or "pinned" to the dataset the
// conversation saves to. The conversation's own choice wins (action=auto,
// action=pin, or a switch, which pins); otherwise the configured `recallMode`
// applies (default auto). The dataset name never implies a mode. A choice
// without a switch is stored as a mode only (a RecallChoice record, no
// dataset), so saves keep following the configured dataset. An override saved
// before the mode existed stays pinned. Multi-scope mode does not route, so
// recall there stays on its scopes; `current` reports the mode in effect.
// ---------------------------------------------------------------------------

import type { CogneePluginConfig, DatasetOverride, DatasetOverridesFile, RecallChoice, RetiredSession } from "./types.js";
import { DATASET_OVERRIDES_PATH, loadDatasetOverrides, saveDatasetOverrides } from "./persistence.js";
import { MAX_PREVIOUS_DATASETS, MAX_SYNCED_RETIRED, pruneRetired } from "./retired.js";
import { isMultiScopeEnabled } from "./scope.js";
import { resolveRecallMode } from "./config.js";
import { jsonResult, type MemoryTool, type MemoryToolContext } from "./tools.js";

export { loadDatasetOverrides, saveDatasetOverrides, pruneRetired, MAX_SYNCED_RETIRED, MAX_PREVIOUS_DATASETS };
export type { DatasetOverride, DatasetOverridesFile, RecallChoice, RetiredSession };

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

/**
 * Names accepted by the switch command: letters, digits, `-` `_`, 1–128 chars.
 * No dots: cognee rejects dataset names containing a space or a dot.
 */
export function isValidDatasetName(name: string): boolean {
  return /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(name);
}

/**
 * True when the config pins recall (SDK-1393): `recallMode: "pinned"`. The
 * dataset name plays no part — a configured `datasetName`, default or not,
 * leaves recall on auto unless recallMode says otherwise.
 */
export function configPinsRecall(cfg: Pick<CogneePluginConfig, "recallMode">): boolean {
  return resolveRecallMode(cfg.recallMode) === "pinned";
}

export function withSessionSuffix(baseSessionId: string, override: DatasetOverride | undefined): string {
  if (!baseSessionId || !override || override.sessionSuffix < 2) return baseSessionId;
  return `${baseSessionId}__${override.sessionSuffix}`;
}

type ConvoKeys = { sessionKey?: string; sessionId?: string };

/** True for a real dataset switch (as opposed to a recall-mode-only choice). */
export function isDatasetSwitch(v: DatasetOverride | RecallChoice | undefined): v is DatasetOverride {
  return !!v && typeof (v as DatasetOverride).dataset === "string" && !!(v as DatasetOverride).dataset;
}

/**
 * In-memory view of the overrides with lazy load and serialized saves. Reads
 * are synchronous so the hot path (capture, recall) never waits on disk; the
 * plugin awaits `ready()` once per hook before consulting it.
 */
const sharedStores = new Map<string, DatasetSwitchStore>();

export class DatasetSwitchStore {
  /**
   * One store per overrides file within this process. The plugin's register()
   * can run more than once in a gateway (plugin loaded via two module
   * specifiers, agent-scoped instances); two instances over one file would be
   * last-writer-wins, so every register() shares the same in-memory state.
   * Cross-process sharing of ~/.openclaw/memory/cognee is not supported —
   * the same holds for every other state file the plugin keeps there.
   */
  static shared(opts: ConstructorParameters<typeof DatasetSwitchStore>[0] = {}): DatasetSwitchStore {
    const key = opts.path ?? DATASET_OVERRIDES_PATH;
    let store = sharedStores.get(key);
    if (!store) {
      store = new DatasetSwitchStore(opts);
      sharedStores.set(key, store);
    }
    return store;
  }

  /** Drop the shared instances (tests: each harness must start from a clean store). */
  static resetShared(): void {
    sharedStores.clear();
  }

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

  /** The first record stored under one of the conversation's keys. */
  private entry(ctx: ConvoKeys | undefined): DatasetOverride | RecallChoice | undefined {
    for (const k of conversationKeys(ctx)) {
      const v = this.data[k];
      if (v) return v;
    }
    return undefined;
  }

  /**
   * Store `record` (or drop the record, when undefined) under every key of the
   * conversation, plus every key still sharing the record it replaces — so the
   * `key:` and `sid:` aliases never disagree, also after a reload made them
   * separate objects.
   */
  private write(ctx: ConvoKeys, record: DatasetOverride | RecallChoice | undefined): void {
    const keys = new Set(conversationKeys(ctx));
    const replaced = new Set([...keys].map((k) => this.data[k]).filter((v) => v !== undefined));
    for (const [k, v] of Object.entries(this.data)) if (replaced.has(v)) keys.add(k);
    for (const k of keys) {
      if (record) this.data[k] = record;
      else delete this.data[k];
    }
    this.persist();
  }

  private nowIso(): string {
    return (this.opts.now ?? (() => new Date()))().toISOString();
  }

  /**
   * The conversation's dataset switch, if any. A recall-mode-only choice is
   * not one: saves, session ids and pinned recall then follow the config.
   */
  get(ctx: ConvoKeys | undefined): DatasetOverride | undefined {
    for (const k of conversationKeys(ctx)) {
      const v = this.data[k];
      if (isDatasetSwitch(v)) return v;
    }
    return undefined;
  }

  /** True when the conversation has a switch or a recall-mode choice (what reset clears). */
  has(ctx: ConvoKeys | undefined): boolean {
    return this.entry(ctx) !== undefined;
  }

  /**
   * Record a switch; returns the new override. `retiring` is the session that
   * was active until now and whether its cache was bridged — an unsynced one
   * is re-synced at session end (or on reset) so `force` never loses turns.
   * A switch pins recall to the new dataset.
   */
  set(
    ctx: ConvoKeys,
    dataset: string,
    currentDataset: string,
    retiring?: { sessionId: string; synced: boolean },
  ): DatasetOverride {
    const existing = this.get(ctx);
    const now = this.nowIso();
    const previous = [...(existing?.previous ?? []), currentDataset].slice(-MAX_PREVIOUS_DATASETS);
    const retired = pruneRetired([
      ...(existing?.retired ?? []),
      ...(retiring?.sessionId ? [{ dataset: currentDataset, sessionId: retiring.sessionId, synced: retiring.synced, retiredAt: now }] : []),
    ]);
    const override: DatasetOverride = {
      dataset,
      sessionSuffix: Math.max(existing?.sessionSuffix ?? 1, 1) + 1,
      switchedAt: now,
      previous,
      retired,
      recall: "pinned",
    };
    this.write(ctx, override);
    return override;
  }

  /**
   * The recall mode this conversation chose, if any: a switch's mode (pinned
   * when saved before the mode existed) or a recall-mode-only choice.
   * undefined → the configured recallMode applies.
   */
  recallMode(ctx: ConvoKeys | undefined): "auto" | "pinned" | undefined {
    const e = this.entry(ctx);
    if (!e) return undefined;
    if (isDatasetSwitch(e)) return e.recall === "auto" ? "auto" : "pinned";
    return e.recall;
  }

  /**
   * True when this conversation's graph recall is restricted to its dataset:
   * its own choice, else `pinnedByConfig` (see configPinsRecall).
   */
  recallPinned(ctx: ConvoKeys | undefined, pinnedByConfig = false): boolean {
    const mode = this.recallMode(ctx);
    return mode ? mode === "pinned" : pinnedByConfig;
  }

  /**
   * Record the conversation's recall mode. It never creates or moves a
   * dataset: on a switch it sets the switch's mode, otherwise it stores a
   * mode-only choice. Returns false when that mode was already recorded (or
   * the context names no conversation).
   */
  setRecall(ctx: ConvoKeys, mode: "auto" | "pinned"): boolean {
    if (conversationKeys(ctx).length === 0 || this.recallMode(ctx) === mode) return false;
    const existing = this.get(ctx);
    this.write(ctx, existing ? { ...existing, recall: mode } : { recall: mode, chosenAt: this.nowIso() });
    return true;
  }

  /** Retired sessions whose switch-time sync failed. */
  unsyncedRetired(ctx: ConvoKeys | undefined): RetiredSession[] {
    return (this.get(ctx)?.retired ?? []).filter((r) => !r.synced);
  }

  /** Flag a retired session as bridged (after a successful later sync). */
  markRetiredSynced(ctx: ConvoKeys, sessionId: string): void {
    const override = this.get(ctx);
    if (!override || !override.retired.some((r) => r.sessionId === sessionId && !r.synced)) return;
    const retired = pruneRetired(override.retired.map((r) => (r.sessionId === sessionId ? { ...r, synced: true } : r)));
    this.write(ctx, { ...override, retired });
  }

  /** Drop a conversation's switch and recall choice (back to the config). */
  clear(ctx: ConvoKeys): boolean {
    if (!this.has(ctx)) return false;
    this.write(ctx, undefined);
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
      enum: ["list", "current", "switch", "pin", "auto", "reset"],
      description: "list = datasets available on the server; current = the dataset this conversation writes to and the recall mode in effect; switch = move this conversation to `dataset` (created if missing, new session) and pin recall to it (switching to the dataset in use is the same as pin); pin = restrict recall to the dataset this conversation saves to, without a new session; auto = unpin recall so the server can route each query (saves stay where they are; also lifts recallMode \"pinned\" from the config, for this conversation); reset = drop this conversation's switch and recall choice and return to the configured dataset and recallMode.",
    },
    dataset: { type: "string", description: "switch only: target dataset name (letters, digits, - _; \"auto\" is reserved, use action=auto)." },
    force: {
      type: "boolean",
      description: "switch/reset only: proceed even if syncing the current session into its dataset fails. The unsynced session is recorded and re-synced at session end (and on reset); until then its turns exist only in the server's session cache and are lost if that cache expires first. Ask the user before setting this.",
    },
  },
  required: ["action"],
  additionalProperties: false,
} as const;

export type MemorySwitchDatasetParams = {
  action: "list" | "current" | "switch" | "pin" | "auto" | "reset";
  dataset?: string;
  force?: boolean;
};

/**
 * Why recall is pinned: the conversation picked it (pin, or a switch),
 * the config says `recallMode: "pinned"`, or multi-scope mode (which never
 * routes). Auto recall carries no reason.
 */
export type RecallReason = "dataset_picked" | "configured_pin" | "multi_scope";
export type RecallState = { recall: "auto" | "pinned"; reason?: RecallReason };

/**
 * The one sentence every memory_switch_dataset result uses to describe the
 * recall mode in effect, so current / pin / auto / switch / reset read alike.
 * `lead` says what the action did; `extra` adds warnings after the mode.
 */
export function recallNote(
  state: RecallState,
  opts: { dataset: string; scopes?: readonly string[]; lead?: string; extra?: string[] },
): string {
  const d = `"${opts.dataset}"`;
  let mode: string;
  if (state.recall === "auto") {
    mode = `Recall is auto: each query goes to the best-matching dataset when the server can tell, otherwise to every readable dataset; saves go to ${d}. action=pin restricts recall to ${d}.`;
  } else if (state.reason === "multi_scope") {
    mode = `Recall is pinned to the configured scopes (${(opts.scopes ?? []).join(", ")}): multi-scope mode does not route; saves go to ${d}.`;
  } else if (state.reason === "configured_pin") {
    mode = `Recall is pinned to ${d}, where this conversation saves, by the plugin config (recallMode "pinned"); action=auto unpins it for this conversation.`;
  } else {
    mode = `Recall is pinned to ${d}, where this conversation saves, by this conversation's choice; action=auto unpins it.`;
  }
  return [opts.lead, mode, ...(opts.extra ?? [])].filter((x): x is string => !!x).join(" ");
}

type RecallFields = { recall: "auto" | "pinned"; reason?: RecallReason; note: string };
type ModeResult = { action: "auto" | "pin"; dataset: string; changed: boolean } & RecallFields;

export type MemorySwitchDatasetResult =
  | { action: "list"; current: string; datasets: Array<{ name: string; id: string; current: boolean }>; note?: string; error?: string }
  | ({ action: "current"; dataset: string; sessionId?: string; switched: boolean; scopes?: string[]; previous: string[] } & RecallFields)
  | ModeResult
  | {
      action: "switch";
      switched: boolean;
      dataset: string;
      sessionId?: string;
      previous?: { dataset: string; sessionId?: string; synced: boolean };
      /** Switching to the dataset in use pins instead: whether the mode changed. */
      changed?: boolean;
      recall?: "auto" | "pinned";
      reason?: RecallReason;
      error?: string;
      note?: string;
    }
  | ({ action: "reset"; reset: boolean; dataset: string; resynced?: string[]; unsynced?: string[]; error?: string } & Partial<RecallFields>);

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
  const hasConvo = !!(ctx.sessionId || ctx.sessionKey);
  const NO_CONVO = "No conversation/session in context, and the recall mode is per conversation; nothing was changed.";

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

  const multiScope = isMultiScopeEnabled(deps.cfg);

  /** The recall mode in effect: multi-scope, else the conversation's choice, else the config. */
  function effective(): RecallState {
    if (multiScope) return { recall: "pinned", reason: "multi_scope" };
    const own = deps.store.recallMode(convo);
    if (own) return own === "pinned" ? { recall: "pinned", reason: "dataset_picked" } : { recall: "auto" };
    return configPinsRecall(deps.cfg) ? { recall: "pinned", reason: "configured_pin" } : { recall: "auto" };
  }

  /** recall / reason / note for a result, with `lead` saying what the action did. */
  function recallFields(dataset: string, lead?: string, extra?: string[]): RecallFields {
    const state = effective();
    return {
      recall: state.recall,
      ...(state.reason ? { reason: state.reason } : {}),
      note: recallNote(state, { dataset, scopes: deps.cfg.recallScopes, lead, extra }),
    };
  }

  async function current(lead?: string): Promise<MemorySwitchDatasetResult> {
    const o = deps.store.get(convo);
    const dataset = deps.currentDataset(ctx);
    const sessionId = deps.currentSessionId(ctx);
    return {
      action: "current",
      dataset,
      ...(sessionId ? { sessionId } : {}),
      switched: !!o,
      ...(multiScope ? { scopes: [...deps.cfg.recallScopes] } : {}),
      previous: o?.previous ?? [],
      ...recallFields(dataset, [lead, o ? "This conversation was switched away from its configured dataset; action=reset returns to it." : undefined].filter(Boolean).join(" ") || undefined),
    };
  }

  /** Record this conversation's recall mode (pin / auto). Never moves saves. */
  function setMode(action: "pin" | "auto"): ModeResult {
    const mode = action === "pin" ? "pinned" : "auto";
    const dataset = deps.currentDataset(ctx);
    const before = effective().recall;
    if (multiScope) {
      return { action, dataset, changed: false, ...recallFields(dataset, "Nothing changed: the recall mode is not set per conversation in multi-scope mode.") };
    }
    if (!hasConvo) return { action, dataset, changed: false, ...recallFields(dataset, NO_CONVO) };
    deps.store.setRecall(convo, mode);
    const changed = effective().recall !== before;
    const lead = changed
      ? (mode === "pinned" ? "Recall pinned for this conversation (same session, saves unchanged)." : "Recall unpinned for this conversation (saves unchanged).")
      : (mode === "pinned" ? "Recall was already pinned." : "Recall was already auto.");
    return { action, dataset, changed, ...recallFields(dataset, lead) };
  }

  async function doSwitch(params: MemorySwitchDatasetParams): Promise<MemorySwitchDatasetResult> {
    const target = typeof params.dataset === "string" ? params.dataset.trim() : "";
    const before = deps.currentDataset(ctx);
    if (!target) return { action: "switch", switched: false, dataset: before, error: "dataset is required" };
    if (target.toLowerCase() === "auto") {
      return { action: "switch", switched: false, dataset: before, error: `"auto" is reserved, not a dataset name — call action="auto" to unpin recall, or pick another dataset name` };
    }
    if (!isValidDatasetName(target)) {
      return { action: "switch", switched: false, dataset: before, error: `invalid dataset name "${target}" — use letters, digits, '-' or '_' (max 128 chars; cognee rejects spaces and dots)` };
    }
    if (!ctx.sessionId && !ctx.sessionKey) {
      return { action: "switch", switched: false, dataset: before, error: "no conversation/session in context — dataset switching is per conversation" };
    }
    if (target === before) {
      // The dataset in use: the same as action=pin (no new session).
      return { ...setMode("pin"), action: "switch", switched: false };
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

    // 3. Record the override (and the retired session) so later hooks — and
    //    the session-end re-sync of anything left unsynced — pick it up.
    deps.store.set(convo, target, before, prevSid ? { sessionId: prevSid, synced } : undefined);
    const newSid = deps.currentSessionId(ctx);
    deps.logger?.debug?.(`cognee-openclaw: conversation switched ${before} -> ${target} (session ${newSid ?? "-"})`);
    return {
      action: "switch",
      switched: true,
      dataset: target,
      ...(newSid ? { sessionId: newSid } : {}),
      previous: { dataset: before, ...(prevSid ? { sessionId: prevSid } : {}), synced },
      ...recallFields(
        target,
        `Switched to "${target}": this conversation now saves there, under a new session. Memory files and shared company/user scopes are unaffected.`,
        synced ? [] : ["The previous session was NOT synced (force); it is re-synced automatically at session end. Tell the user."],
      ),
    };
  }

  /**
   * Re-sync retired sessions whose switch-time sync failed. Returns the ids
   * still unsynced. Used by reset (before dropping the override) and exposed
   * to the plugin's session-end chain via the store.
   */
  async function resyncRetired(): Promise<{ resynced: string[]; unsynced: string[] }> {
    const resynced: string[] = [];
    const unsynced: string[] = [];
    for (const r of deps.store.unsyncedRetired(convo)) {
      try {
        await deps.syncSession(r.dataset, r.sessionId);
        deps.store.markRetiredSynced(convo, r.sessionId);
        resynced.push(r.sessionId);
      } catch (e) {
        deps.logger?.warn?.(`cognee-openclaw: re-sync of retired session ${r.sessionId} into "${r.dataset}" failed: ${String(e)}`);
        unsynced.push(r.sessionId);
      }
    }
    return { resynced, unsynced };
  }

  async function reset(params: MemorySwitchDatasetParams): Promise<MemorySwitchDatasetResult> {
    if (!deps.store.has(convo)) {
      const dataset = deps.currentDataset(ctx);
      return { action: "reset", reset: false, dataset, ...recallFields(dataset, "This conversation already uses the configured dataset and recall mode.") };
    }
    // Dropping the override would also drop the record of unsynced retired
    // sessions, so bridge them first; refuse (without force) if any still fail.
    const { resynced, unsynced } = await resyncRetired();
    if (unsynced.length > 0 && !params.force) {
      return {
        action: "reset",
        reset: false,
        dataset: deps.currentDataset(ctx),
        resynced,
        unsynced,
        error: `${unsynced.length} retired session(s) could not be synced into their dataset; nothing was changed. Retry, or call again with force=true only if the user accepts that those turns may be lost.`,
      };
    }
    if (unsynced.length > 0) deps.logger?.warn?.(`cognee-openclaw: reset with force — ${unsynced.length} retired session(s) left unsynced: ${unsynced.join(", ")}`);
    deps.store.clear(convo);
    const dataset = deps.currentDataset(ctx);
    return {
      action: "reset",
      reset: true,
      dataset,
      ...(resynced.length > 0 ? { resynced } : {}),
      ...(unsynced.length > 0 ? { unsynced } : {}),
      ...recallFields(
        dataset,
        "Back to the configured dataset and recall mode.",
        unsynced.length > 0 ? ["Reset forced; the listed sessions were not bridged into the graph. Tell the user."] : [],
      ),
    };
  }

  return {
    name: "memory_switch_dataset",
    label: "Memory Switch Dataset",
    description:
      "Move this conversation to another Cognee dataset, choose how it recalls, or inspect both. Auto recall is routed by the server (cognee >= 1.6.6), best effort: it picks the best-matching dataset when it can, and otherwise searches every readable dataset (as an older server always does); pinned recall reads only the dataset this conversation saves to; multi-scope mode keeps recall on its configured scopes. The conversation's choice wins; otherwise the plugin config's recallMode applies (default auto). action=list shows available datasets (present them and let the user pick; unknown names are created); action=switch syncs the current session, then binds this conversation to `dataset` for all later saves and pins recall to it; action=pin pins recall to the dataset in use; action=auto unpins recall; action=current reports the active dataset and the recall mode in effect; action=reset drops this conversation's switch and recall choice. " +
      "Switching is per conversation and does not move memory files or shared company/user memory.",
    parameters: MemorySwitchDatasetSchema as unknown as Record<string, unknown>,
    async execute(_id, params) {
      // A fresh gateway may not have loaded the saved overrides yet.
      await deps.store.ready();
      switch (params?.action) {
        case "list": return jsonResult(await list());
        case "current": return jsonResult(await current());
        case "switch": return jsonResult(await doSwitch(params));
        case "pin": return jsonResult(setMode("pin"));
        case "auto": return jsonResult(setMode("auto"));
        case "reset": return jsonResult(await reset(params));
        default: return jsonResult(await current("action must be list, current, switch, pin, auto or reset."));
      }
    },
  };
}
