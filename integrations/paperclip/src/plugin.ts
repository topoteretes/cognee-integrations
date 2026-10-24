/**
 * The plugin definition. `createPlugin()` wires the ledger, outbox, capture
 * handlers, tools, jobs and operator actions onto a `PluginContext`; the
 * worker entrypoint calls it once and hands the result to `runWorker`.
 * Options exist for tests (an in-memory ledger, a fixed clock).
 */
import { definePlugin, type PluginContext, type PluginEvent, type PaperclipPlugin } from "@paperclipai/plugin-sdk";
import { ACTION_CLEANUP_SCOPE, ACTION_SYNC_PROJECT, DATA_HEALTH, JOB_DRAIN, JOB_RECONCILE, TOOL_RECALL, TOOL_REMEMBER, manifest } from "./manifest.js";
import { DEFAULTS, parseConfig, resolveConfig, validateConfig } from "./config.js";
import { CogneeClient } from "./cognee-client.js";
import { describeError } from "./errors.js";
import { handleCommentCreated, handleIssueUpdated, syncProject } from "./capture.js";
import { InMemoryLedger, PluginDbLedger, type Clock, type Ledger } from "./ledger.js";
import { Outbox, type BackendResolver, type CompanyBackend } from "./outbox.js";
import { ScopePolicy } from "./scope.js";
import { recallTool, rememberDecisionTool } from "./tools.js";
import { PACKAGE_VERSION } from "./version.js";

export interface CreatePluginOptions {
  /** Ledger implementation; defaults to the plugin-db ledger. Tests pass `new InMemoryLedger()`. */
  ledger?: Ledger;
  clock?: Clock;
  /** Send immediately after enqueueing from an event (default true). */
  drainOnEvent?: boolean;
}

export interface PluginRuntime {
  ledger: Ledger;
  outbox: Outbox;
  backend: BackendResolver;
}

export interface CreatedPlugin {
  /** The sealed plugin for `runWorker` / the host. */
  plugin: PaperclipPlugin;
  /** The ledger/outbox/backend wired in `setup`, for tests and operator tooling (null before setup). */
  runtime: () => PluginRuntime | null;
}

export function createPlugin(options: CreatePluginOptions = {}): CreatedPlugin {
  const clock = options.clock ?? (() => new Date());
  let runtime: PluginRuntime | null = null;
  let setupCtx: PluginContext | null = null;

  const plugin = definePlugin({
    multiCompanyConfig: true,

    async setup(ctx) {
      setupCtx = ctx;
      const ledger = options.ledger ?? (ctx.manifest.database ? new PluginDbLedger(ctx.db, clock) : new InMemoryLedger(clock));
      if (!options.ledger && !ctx.manifest.database) ctx.logger.warn("no database namespace declared: ledger is in-memory and does not survive a restart");

      const backend: BackendResolver = async (companyId: string): Promise<CompanyBackend> => {
        const config = resolveConfig(await ctx.config.get(companyId));
        // Resolved per call and handed straight to the client; never cached, never logged.
        const apiKey = await ctx.secrets.resolve(config.cogneeApiKeyRef as never, { companyId, configPath: "cogneeApiKeyRef" });
        const client = new CogneeClient({
          baseUrl: config.cogneeBaseUrl,
          apiKey,
          fetch: (url, init) => ctx.http.fetch(url, init),
          requestTimeoutMs: DEFAULTS.statusTimeoutMs,
          maxResponseBytes: DEFAULTS.maxResponseBytes,
        });
        return { config, client, scope: new ScopePolicy(config) };
      };

      const outbox = new Outbox({
        ledger,
        backend,
        logger: ctx.logger,
        clock,
        sourceExists: async (row) => {
          if (row.sourceType === "issue") return (await ctx.issues.get(row.sourceId, row.companyId)) !== null;
          if (row.sourceType === "comment") {
            // A comment's issue id is in the row's record text ("Task id: <id>; comment id: <id>").
            const match = /^Task id: ([^;\n]+); comment id: /m.exec(row.content ?? "");
            if (!match) return undefined;
            const comments = await ctx.issues.listComments(match[1].trim(), row.companyId);
            const comment = comments.find((entry) => entry.id === row.sourceId);
            return !!comment && !comment.deletedAt;
          }
          return undefined;
        },
      });
      runtime = { ledger, outbox, backend };
      const deps = { ctx, outbox, drainNow: options.drainOnEvent !== false };

      ctx.events.on("issue.updated", async (event: PluginEvent) => {
        try {
          const outcome = await handleIssueUpdated(deps, event);
          ctx.logger.debug("issue.updated", { companyId: event.companyId, ...outcome });
        } catch (error) {
          ctx.logger.error("issue.updated handler failed", { companyId: event.companyId, error: describeError(error) });
        }
      });
      ctx.events.on("issue.comment.created", async (event: PluginEvent) => {
        try {
          const outcome = await handleCommentCreated(deps, event);
          ctx.logger.debug("issue.comment.created", { companyId: event.companyId, ...outcome });
        } catch (error) {
          ctx.logger.error("issue.comment.created handler failed", { companyId: event.companyId, error: describeError(error) });
        }
      });

      ctx.jobs.register(JOB_DRAIN, async (job) => {
        const summary = await outbox.drainAll();
        ctx.logger.info("drain-outbox finished", { runId: job.runId, ...summary, errors: summary.errors.length });
        await ctx.state.set({ scopeKind: "instance", stateKey: "last-drain" }, { at: clock().toISOString(), ...summary });
      });
      ctx.jobs.register(JOB_RECONCILE, async (job) => {
        const summary = await outbox.reconcileAll();
        ctx.logger.info("reconcile-sources finished", { runId: job.runId, ...summary, errors: summary.errors.length });
        await ctx.state.set({ scopeKind: "instance", stateKey: "last-reconcile" }, { at: clock().toISOString(), ...summary });
      });

      const toolDeps = { ctx, outbox, backend, clock };
      const recallDecl = manifest.tools!.find((tool) => tool.name === TOOL_RECALL)!;
      const rememberDecl = manifest.tools!.find((tool) => tool.name === TOOL_REMEMBER)!;
      ctx.tools.register(TOOL_RECALL, recallDecl, (params, runCtx) => recallTool(toolDeps, params, runCtx));
      ctx.tools.register(TOOL_REMEMBER, rememberDecl, (params, runCtx) => rememberDecisionTool(toolDeps, params, runCtx));

      ctx.actions.register(ACTION_SYNC_PROJECT, async (params, context) => {
        const companyId = typeof params.companyId === "string" ? params.companyId : context.actor?.companyId ?? null;
        const projectId = typeof params.projectId === "string" ? params.projectId : "";
        if (!companyId || !projectId) throw new Error("companyId and projectId are required");
        return syncProject(deps, companyId, projectId);
      });
      ctx.actions.register(ACTION_CLEANUP_SCOPE, async (params, context) => {
        const companyId = typeof params.companyId === "string" ? params.companyId : context.actor?.companyId ?? null;
        const projectId = typeof params.projectId === "string" ? params.projectId : "";
        if (!companyId || !projectId) throw new Error("companyId and projectId are required");
        if (params.confirm !== true) throw new Error("cleanup-scope deletes every plugin-owned document of the project; pass confirm: true");
        const summary = await outbox.cleanupScope(companyId, projectId);
        await ctx.activity.log({ companyId, message: `Company memory cleanup for project ${projectId}: ${summary.deleted} documents deleted, ${summary.revoked} queued writes revoked`, metadata: { ...summary } }).catch(() => undefined);
        return summary;
      });

      ctx.data.register(DATA_HEALTH, async (params) => {
        const companyId = typeof params.companyId === "string" ? params.companyId : undefined;
        return { version: PACKAGE_VERSION, counts: await ledger.countByStatus(companyId), lastDrain: outbox.lastDrain, lastReconcile: outbox.lastReconcile };
      });

      ctx.logger.info("cognee company memory ready", { version: PACKAGE_VERSION, ledger: options.ledger ? "injected" : ctx.manifest.database ? "plugin-db" : "memory" });
    },

    async onHealth() {
      if (!runtime) return { status: "error", message: "setup has not run" };
      const counts = await runtime.ledger.countByStatus();
      const conflicts = counts.conflict ?? 0;
      const failed = counts.failed ?? 0;
      const pending = (counts.pending ?? 0) + (counts.uncertain ?? 0) + (counts.superseded ?? 0);
      const status = conflicts > 0 || failed > 0 ? "degraded" : "ok";
      const message = status === "ok" ? `ledger healthy; ${pending} write(s) pending` : `${conflicts} conflict(s) and ${failed} failed write(s) need an operator`;
      return { status, message, details: { counts, lastDrain: runtime.outbox.lastDrain, lastReconcile: runtime.outbox.lastReconcile } };
    },

    async onValidateConfig(config) {
      const result = validateConfig(config);
      if (!result.ok || !setupCtx) return result;
      // Test connection: reach the backend with the configured credentials.
      try {
        const { config: parsed } = parseConfig(config);
        const apiKey = await setupCtx.secrets.resolve(parsed.cogneeApiKeyRef as never, { configPath: "cogneeApiKeyRef" });
        const client = new CogneeClient({ baseUrl: parsed.cogneeBaseUrl, apiKey, fetch: (url, init) => setupCtx!.http.fetch(url, init), requestTimeoutMs: DEFAULTS.statusTimeoutMs });
        const health = await client.health();
        if (!health.reachable || health.status >= 500) return { ok: false, errors: [`cognee /health answered HTTP ${health.status}`], warnings: result.warnings };
        await client.listDatasets();
        return result;
      } catch (error) {
        return { ok: false, errors: [`cognee backend check failed: ${describeError(error)}`], warnings: result.warnings };
      }
    },

    async onConfigChanged() {
      // Configuration is read per call through ctx.config.get(companyId); nothing is cached.
    },
  });

  return { plugin, runtime: () => runtime };
}
