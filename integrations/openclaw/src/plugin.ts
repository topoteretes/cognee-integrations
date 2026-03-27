import { randomUUID } from "node:crypto";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import type { CogneeSearchResult, MemoryScope, ScopedSyncIndexes, SyncIndex } from "./types.js";
import { MEMORY_SCOPES } from "./types.js";
import { CogneeHttpClient } from "./client.js";
import { resolveConfig } from "./config.js";
import { collectMemoryFiles } from "./files.js";
import {
  loadDatasetState,
  loadScopedSyncIndexes,
  loadSyncIndex,
  migrateLegacyIndex,
  SCOPED_SYNC_INDEX_PATH,
  SYNC_INDEX_PATH,
} from "./persistence.js";
import { datasetNameForScope, isMultiScopeEnabled, routeFileToScope } from "./scope.js";
import { syncFiles, syncFilesScoped } from "./sync.js";

// ---------------------------------------------------------------------------
// Plugin registration
// ---------------------------------------------------------------------------

const memoryCogneePlugin = {
  id: "cognee-openclaw",
  name: "Memory (Cognee)",
  description: "Cognee-backed memory with multi-scope support (company/user/agent), session tracking, and auto-recall",
  kind: "memory" as const,
  register(api: OpenClawPluginApi) {
    const cfg = resolveConfig(api.pluginConfig);
    const client = new CogneeHttpClient(cfg.baseUrl, cfg.apiKey, cfg.username, cfg.password, cfg.requestTimeoutMs, cfg.ingestionTimeoutMs);
    const multiScope = isMultiScopeEnabled(cfg);

    // Legacy single-scope state
    let datasetId: string | undefined;
    let syncIndex: SyncIndex = { entries: {} };

    // Multi-scope state
    let scopedIndexes: ScopedSyncIndexes = {};

    // Session state
    let sessionId: string | undefined;

    let resolvedWorkspaceDir: string | undefined;

    // Permission grant callback (fires when a new dataset is created)
    const onNewDataset = cfg.enablePermissionGrants && cfg.grantReadUserIds.length > 0
      ? async (datasetId: string) => {
          await client.grantPermissions({
            datasetId,
            recipientIds: cfg.grantReadUserIds,
            permissionType: "read",

            logger: api.logger,
          });
        }
      : undefined;

    // Load persisted state on startup
    const stateReady = Promise.all([
      loadDatasetState()
        .then((state) => {
          if (!multiScope) {
            datasetId = state[cfg.datasetName];
          }
        })
        .catch((error) => {
          api.logger.warn?.(`cognee-openclaw: failed to load dataset state: ${String(error)}`);
        }),
      multiScope
        ? loadScopedSyncIndexes()
            .then(async (indexes) => {
              // Fix #7: Migrate legacy index if scoped indexes are empty
              if (Object.keys(indexes).length === 0) {
                const migrated = await migrateLegacyIndex(cfg.defaultWriteScope);
                if (migrated) {
                  scopedIndexes = migrated;
                  api.logger.info?.(`cognee-openclaw: migrated legacy sync index to scope "${cfg.defaultWriteScope}"`);
                  return;
                }
              }
              scopedIndexes = indexes;
            })
            .catch((error) => {
              api.logger.warn?.(`cognee-openclaw: failed to load scoped sync indexes: ${String(error)}`);
            })
        : loadSyncIndex()
            .then((state) => {
              syncIndex = state;
              if (!datasetId && state.datasetId && state.datasetName === cfg.datasetName) {
                datasetId = state.datasetId;
              }
            })
            .catch((error) => {
              api.logger.warn?.(`cognee-openclaw: failed to load sync index: ${String(error)}`);
            }),
    ]);

    // Fix #8: Log when scopes have no dataset ID during recall
    async function getRecallDatasetIds(): Promise<{ ids: string[]; missingScopes: string[] }> {
      const state = await loadDatasetState();
      const ids: string[] = [];
      const missingScopes: string[] = [];

      if (multiScope) {
        for (const scope of cfg.recallScopes) {
          const dsName = datasetNameForScope(scope, cfg);
          const dsId = state[dsName] ?? scopedIndexes[scope]?.datasetId;
          if (dsId) {
            ids.push(dsId);
          } else {
            missingScopes.push(scope);
          }
        }
      } else {
        if (datasetId) ids.push(datasetId);
      }

      return { ids, missingScopes };
    }

    // Helper: run sync
    async function runSync(workspaceDir: string, logger: { info?: (msg: string) => void; warn?: (msg: string) => void }) {
      await stateReady;

      const files = await collectMemoryFiles(workspaceDir);
      if (files.length === 0) {
        logger.info?.("cognee-openclaw: no memory files found");
        return { added: 0, updated: 0, skipped: 0, errors: 0, deleted: 0 };
      }

      logger.info?.(`cognee-openclaw: found ${files.length} memory file(s), syncing...`);

      if (multiScope) {
        return syncFilesScoped(client, files, files, scopedIndexes, cfg, logger, onNewDataset ? (dsId) => onNewDataset(dsId) : undefined);
      } else {
        const result = await syncFiles(client, files, files, syncIndex, cfg, logger, undefined, onNewDataset);
        if (result.datasetId) datasetId = result.datasetId;
        return result;
      }
    }

    // ------------------------------------------------------------------
    // CLI commands
    // ------------------------------------------------------------------

    api.registerCli((ctx) => {
      const cognee = ctx.program.command("cognee").description("Cognee memory management");
      const cliWorkspaceDir = ctx.workspaceDir || process.cwd();

      cognee
        .command("index")
        .description("Sync memory files to Cognee (add new, update changed, skip unchanged)")
        .action(async () => {
          const result = await runSync(cliWorkspaceDir, ctx.logger);
          const summary = `Sync complete: ${result.added} added, ${result.updated} updated, ${result.deleted} deleted, ${result.skipped} unchanged, ${result.errors} errors`;
          ctx.logger.info?.(summary);
          console.log(summary);
          process.exit(0);
        });

      cognee
        .command("status")
        .description("Show Cognee sync state")
        .action(async () => {
          await stateReady;
          const files = await collectMemoryFiles(cliWorkspaceDir);

          if (multiScope) {
            const state = await loadDatasetState();
            for (const scope of MEMORY_SCOPES) {
              const dsName = datasetNameForScope(scope, cfg);
              const scopeIndex = scopedIndexes[scope] ?? { entries: {} };
              const entryCount = Object.keys(scopeIndex.entries).length;
              const scopeFiles = files.filter(f =>
                routeFileToScope(f.path, cfg.scopeRouting, cfg.defaultWriteScope) === scope
              );
              let dirty = 0, newCount = 0;
              for (const file of scopeFiles) {
                const existing = scopeIndex.entries[file.path];
                if (!existing) newCount++;
                else if (existing.hash !== file.hash) dirty++;
              }
              console.log(`\n[${scope.toUpperCase()}] Dataset: ${dsName}`);
              console.log(`  Dataset ID: ${state[dsName] ?? scopeIndex.datasetId ?? "(not set)"}`);
              console.log(`  Indexed files: ${entryCount}`);
              console.log(`  Workspace files: ${scopeFiles.length}`);
              console.log(`  New (unindexed): ${newCount}`);
              console.log(`  Changed (dirty): ${dirty}`);
            }
          } else {
            const entryCount = Object.keys(syncIndex.entries).length;
            const entriesWithDataId = Object.values(syncIndex.entries).filter((e) => e.dataId).length;
            let dirty = 0, newCount = 0;
            for (const file of files) {
              const existing = syncIndex.entries[file.path];
              if (!existing) newCount++;
              else if (existing.hash !== file.hash) dirty++;
            }
            console.log([
              `Dataset: ${syncIndex.datasetName ?? cfg.datasetName}`,
              `Dataset ID: ${datasetId ?? syncIndex.datasetId ?? "(not set)"}`,
              `Indexed files: ${entryCount} (${entriesWithDataId} with data ID)`,
              `Workspace files: ${files.length}`,
              `New (unindexed): ${newCount}`,
              `Changed (dirty): ${dirty}`,
              `Sync index: ${SYNC_INDEX_PATH}`,
            ].join("\n"));
          }
          process.exit(0);
        });

      cognee
        .command("health")
        .description("Check Cognee API connectivity")
        .action(async () => {
          try {
            const result = await client.health();
            console.log(`Cognee API: OK (${cfg.baseUrl})`);
            if (result.status) console.log(`Status: ${result.status}`);
          } catch (error) {
            console.log(`Cognee API: UNREACHABLE (${cfg.baseUrl})`);
            console.log(`Error: ${error instanceof Error ? error.message : String(error)}`);
            process.exit(1);
          }
          process.exit(0);
        });

      cognee
        .command("setup")
        .description("Configure OpenClaw to use Cognee for memory (default: replaces built-in, --hybrid: alongside built-in)")
        .option("--hybrid", "Keep built-in memory providers enabled alongside Cognee")
        .action(async (opts: { hybrid?: boolean }) => {
          const { loadConfig, writeConfigFile } = api.runtime.config;
          const config = loadConfig();

          // Set Cognee as the memory slot
          config.plugins ??= {} as typeof config.plugins;
          config.plugins.slots ??= {} as typeof config.plugins.slots;
          (config.plugins.slots as Record<string, string>).memory = "cognee-openclaw";

          config.plugins.entries ??= {} as typeof config.plugins.entries;
          const entries = config.plugins.entries as Record<string, { enabled: boolean }>;

          if (opts.hybrid) {
            // Hybrid mode: keep built-in memory enabled
            entries["memory-core"] ??= { enabled: true } as typeof entries[string];
            entries["memory-core"].enabled = true;
          } else {
            // Exclusive mode: disable built-in memory providers
            entries["memory-core"] = { enabled: false };
            entries["memory-lancedb"] = { enabled: false };
          }

          // Ensure cognee-openclaw is enabled
          entries["cognee-openclaw"] ??= { enabled: true } as typeof entries[string];
          entries["cognee-openclaw"].enabled = true;

          await writeConfigFile(config);

          if (opts.hybrid) {
            console.log("Cognee memory setup complete (hybrid mode):");
            console.log("  - Memory slot set to cognee-openclaw");
            console.log("  - memory-core enabled (built-in memory active)");
            console.log("\nBoth Cognee recall and built-in memory search are active.");
          } else {
            console.log("Cognee memory setup complete:");
            console.log("  - Memory slot set to cognee-openclaw");
            console.log("  - memory-core disabled");
            console.log("  - memory-lancedb disabled");
          }
          console.log("\nRun 'openclaw cognee health' to verify Cognee connectivity.");
          process.exit(0);
        });

      cognee
        .command("scopes")
        .description("Show memory scope routing for current workspace files")
        .action(async () => {
          const files = await collectMemoryFiles(cliWorkspaceDir);
          if (files.length === 0) {
            console.log("No memory files found.");
            process.exit(0);
          }
          if (!multiScope) {
            console.log(`Multi-scope mode is OFF. All files go to dataset "${cfg.datasetName}".`);
            console.log(`Set companyDataset, userDatasetPrefix, or agentDatasetPrefix to enable.`);
            process.exit(0);
          }
          const grouped: Record<MemoryScope, string[]> = { company: [], user: [], agent: [] };
          for (const file of files) {
            const scope = routeFileToScope(file.path, cfg.scopeRouting, cfg.defaultWriteScope);
            grouped[scope].push(file.path);
          }
          for (const scope of MEMORY_SCOPES) {
            const dsName = datasetNameForScope(scope, cfg);
            console.log(`\n[${scope.toUpperCase()}] -> dataset "${dsName}"`);
            if (grouped[scope].length === 0) console.log("  (no files)");
            else for (const p of grouped[scope]) console.log(`  ${p}`);
          }
          process.exit(0);
        });

      cognee
        .command("permissions")
        .description("Grant read permissions on all known datasets to configured users")
        .action(async () => {
          if (!cfg.enablePermissionGrants) {
            console.log("Permission grants are disabled. Set enablePermissionGrants: true in plugin config.");
            process.exit(1);
          }
          if (cfg.grantReadUserIds.length === 0) {
            console.log("No users configured. Set grantReadUserIds in plugin config.");
            process.exit(1);
          }
          const state = await loadDatasetState();
          const datasets = Object.entries(state);
          if (datasets.length === 0) {
            console.log("No datasets found. Run 'openclaw cognee index' first.");
            process.exit(0);
          }
          for (const [dsName, dsId] of datasets) {
            console.log(`\nDataset: ${dsName} (${dsId})`);
            await client.grantPermissions({
              datasetId: dsId,
              recipientIds: cfg.grantReadUserIds,
              permissionType: "read",
  
              logger: { info: (m) => console.log(`  ${m}`), warn: (m) => console.log(`  WARN: ${m}`) },
            });
          }
          console.log("\nDone.");
          process.exit(0);
        });
    }, { commands: ["cognee"] });

    // ------------------------------------------------------------------
    // Auto-sync on startup (with health check)
    // ------------------------------------------------------------------

    if (cfg.autoIndex) {
      api.registerService({
        id: "cognee-auto-sync",
        async start(ctx) {
          resolvedWorkspaceDir = ctx.workspaceDir || process.cwd();

          try {
            await client.health();
          } catch (error) {
            ctx.logger.warn?.(`cognee-openclaw: Cognee API unreachable at ${cfg.baseUrl} — auto-sync disabled for this session. Error: ${String(error)}`);
            return;
          }

          if (cfg.enableSessions) {
            sessionId = `openclaw-${randomUUID()}`;
            ctx.logger.info?.(`cognee-openclaw: session ${sessionId}`);
          }

          try {
            const result = await runSync(resolvedWorkspaceDir, ctx.logger);
            ctx.logger.info?.(`cognee-openclaw: auto-sync complete: ${result.added} added, ${result.updated} updated, ${result.deleted} deleted, ${result.skipped} unchanged`);
          } catch (error) {
            ctx.logger.warn?.(`cognee-openclaw: auto-sync failed: ${String(error)}`);
          }

          // Re-grant permissions on all known datasets (idempotent)
          if (cfg.enablePermissionGrants && cfg.grantReadUserIds.length > 0) {
            try {
              const state = await loadDatasetState();
              for (const [, dsId] of Object.entries(state)) {
                await client.grantPermissions({
                  datasetId: dsId,
                  recipientIds: cfg.grantReadUserIds,
                  permissionType: "read",
      
                  logger: ctx.logger,
                });
              }
            } catch (error) {
              ctx.logger.warn?.(`cognee-openclaw: startup permission grants failed: ${String(error)}`);
            }
          }
        },
      });
    }

    // ------------------------------------------------------------------
    // Auto-recall: inject memories before each agent run
    // ------------------------------------------------------------------

    if (cfg.autoRecall) {
      api.on("before_agent_start", async (event, ctx) => {
        await stateReady;

        if (!event.prompt || event.prompt.length < 5) {
          api.logger.debug?.("cognee-openclaw: skipping recall (prompt too short)");
          return;
        }

        const { ids: recallDatasetIds, missingScopes } = await getRecallDatasetIds();

        // Fix #8: Log missing scopes so users know what's not being searched
        if (missingScopes.length > 0) {
          api.logger.info?.(`cognee-openclaw: scope(s) not yet indexed (no data): ${missingScopes.join(", ")}`);
        }

        if (recallDatasetIds.length === 0) {
          api.logger.debug?.("cognee-openclaw: skipping recall (no datasetIds)");
          return;
        }

        try {
          if (multiScope) {
            // Fix #10: Use Promise.allSettled for resilience
            const state = await loadDatasetState();

            const searchPromises = cfg.recallScopes.map(async (scope): Promise<{ scope: MemoryScope; results: CogneeSearchResult[] } | null> => {
              const dsName = datasetNameForScope(scope, cfg);
              const dsId = state[dsName] ?? scopedIndexes[scope]?.datasetId;
              if (!dsId) return null;

              const results = await client.search({
                queryText: event.prompt,
                searchType: cfg.searchType,
                datasetIds: [dsId],
                searchPrompt: cfg.searchPrompt,
                maxTokens: cfg.maxTokens,
                sessionId,
              });

              const filtered = results
                .filter((r) => r.score >= cfg.minScore)
                .slice(0, cfg.maxResults);

              return filtered.length > 0 ? { scope, results: filtered } : null;
            });

            // Fix #10: allSettled — inject whatever succeeds, log failures
            const settled = await Promise.allSettled(searchPromises);
            const scopeResults: Record<string, CogneeSearchResult[]> = {};

            for (let i = 0; i < settled.length; i++) {
              const outcome = settled[i];
              const scope = cfg.recallScopes[i];
              if (outcome.status === "fulfilled" && outcome.value) {
                scopeResults[outcome.value.scope] = outcome.value.results;
              } else if (outcome.status === "rejected") {
                api.logger.warn?.(`cognee-openclaw: recall failed for scope ${scope}: ${String(outcome.reason)}`);
              }
            }

            if (Object.keys(scopeResults).length === 0) {
              api.logger.debug?.("cognee-openclaw: search returned no results above minScore");
              return;
            }

            const sections: string[] = [];
            for (const scope of cfg.recallScopes) {
              const results = scopeResults[scope];
              if (!results || results.length === 0) continue;
              const payload = JSON.stringify(
                results.map((r) => ({ id: r.id, score: r.score, text: r.text, metadata: r.metadata })),
                null, 2,
              );
              sections.push(`<${scope}_memory>\n${payload}\n</${scope}_memory>`);
            }

            const totalResults = Object.values(scopeResults).reduce((sum, arr) => sum + arr.length, 0);
            api.logger.info?.(`cognee-openclaw: injecting ${totalResults} memories across ${Object.keys(scopeResults).length} scope(s)`);

            return { prependContext: `<cognee_memories>\n${sections.join("\n")}\n</cognee_memories>` };
          } else {
            // Legacy single-scope
            const results = await client.search({
              queryText: event.prompt,
              searchType: cfg.searchType,
              datasetIds: recallDatasetIds,
              searchPrompt: cfg.searchPrompt,
              maxTokens: cfg.maxTokens,
              sessionId,
            });

            const filtered = results
              .filter((r) => r.score >= cfg.minScore)
              .slice(0, cfg.maxResults);

            if (filtered.length === 0) {
              api.logger.debug?.("cognee-openclaw: search returned no results above minScore");
              return;
            }

            const payload = JSON.stringify(
              filtered.map((r) => ({ id: r.id, score: r.score, text: r.text, metadata: r.metadata })),
              null, 2,
            );

            api.logger.info?.(`cognee-openclaw: injecting ${filtered.length} memories`);
            return { prependContext: `<cognee_memories>\nRelevant memories:\n${payload}\n</cognee_memories>` };
          }
        } catch (error) {
          api.logger.warn?.(`cognee-openclaw: recall failed: ${String(error)}`);
        }
      });
    }

    // ------------------------------------------------------------------
    // Post-agent sync + session persistence
    // ------------------------------------------------------------------

    if (cfg.autoIndex) {
      api.on("agent_end", async (event, ctx) => {
        if (!event.success) return;
        await stateReady;

        const workspaceDir = resolvedWorkspaceDir || process.cwd();

        // Fix #4: Actually persist the session into the knowledge graph
        if (cfg.enableSessions && cfg.persistSessionsAfterEnd && sessionId) {
          try {
            // Determine target dataset for session persistence
            const targetDatasetIds: string[] = [];
            if (multiScope) {
              const state = await loadDatasetState();
              const agentDsName = datasetNameForScope("agent", cfg);
              const agentDsId = state[agentDsName] ?? scopedIndexes.agent?.datasetId;
              if (agentDsId) targetDatasetIds.push(agentDsId);
            } else if (datasetId) {
              targetDatasetIds.push(datasetId);
            }

            if (targetDatasetIds.length > 0) {
              // Call Cognee's session persistence endpoint via the generic fetchJson
              await client.fetchJson("/api/v1/sessions/persist", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  session_ids: [sessionId],
                  dataset_ids: targetDatasetIds,
                }),
              }).catch(() => {
                // Session persistence endpoint may not exist on all Cognee versions.
                // Fail silently — this is an enhancement, not critical path.
                api.logger.debug?.("cognee-openclaw: session persistence endpoint not available");
              });
            }
          } catch (error) {
            api.logger.warn?.(`cognee-openclaw: session persistence failed: ${String(error)}`);
          }
        }

        // Sync file changes
        try {
          if (multiScope) {
            try {
              const freshIndexes = await loadScopedSyncIndexes();
              scopedIndexes = freshIndexes;
            } catch { /* fall through */ }

            const files = await collectMemoryFiles(workspaceDir);

            let hasChanges = false;
            for (const file of files) {
              const scope = routeFileToScope(file.path, cfg.scopeRouting, cfg.defaultWriteScope);
              const scopeIndex = scopedIndexes[scope];
              if (!scopeIndex) { hasChanges = true; break; }
              const existing = scopeIndex.entries[file.path];
              if (!existing || existing.hash !== file.hash) { hasChanges = true; break; }
            }

            if (!hasChanges) {
              const currentPaths = new Set(files.map(f => f.path));
              for (const scopeIndex of Object.values(scopedIndexes)) {
                if (scopeIndex && Object.keys(scopeIndex.entries).some(p => !currentPaths.has(p))) {
                  hasChanges = true;
                  break;
                }
              }
            }

            if (!hasChanges) return;

            api.logger.info?.("cognee-openclaw: detected changes, syncing across scopes...");
            const result = await syncFilesScoped(client, files, files, scopedIndexes, cfg, api.logger, onNewDataset ? (dsId) => onNewDataset(dsId) : undefined);
            api.logger.info?.(`cognee-openclaw: post-agent sync: ${result.added} added, ${result.updated} updated, ${result.deleted} deleted`);
          } else {
            try {
              const freshIndex = await loadSyncIndex();
              syncIndex.entries = freshIndex.entries;
              if (freshIndex.datasetId) syncIndex.datasetId = freshIndex.datasetId;
              if (freshIndex.datasetName) syncIndex.datasetName = freshIndex.datasetName;
            } catch { /* fall through */ }

            const files = await collectMemoryFiles(workspaceDir);
            const changedFiles = files.filter((f) => {
              const existing = syncIndex.entries[f.path];
              return !existing || existing.hash !== f.hash;
            });

            const currentPaths = new Set(files.map(f => f.path));
            const hasDeletedFiles = Object.keys(syncIndex.entries).some(p => !currentPaths.has(p));

            if (changedFiles.length === 0 && !hasDeletedFiles) return;

            api.logger.info?.(`cognee-openclaw: detected ${changedFiles.length} changed file(s)${hasDeletedFiles ? " + deletions" : ""}, syncing...`);
            const result = await syncFiles(client, changedFiles, files, syncIndex, cfg, api.logger, undefined, onNewDataset);
            if (result.datasetId) datasetId = result.datasetId;
            api.logger.info?.(`cognee-openclaw: post-agent sync: ${result.added} added, ${result.updated} updated, ${result.deleted} deleted`);
          }
        } catch (error) {
          api.logger.warn?.(`cognee-openclaw: post-agent sync failed: ${String(error)}`);
        }
      });
    }
  },
};

export default memoryCogneePlugin;
