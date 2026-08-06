/**
 * Bridge module — session resolution and hook helpers.
 *
 * Replaces the old Python subprocess bridge. Now that all logic is
 * TypeScript, this module just provides session ID mapping and
 * helper functions for the hooks.
 */

import { join, dirname } from "node:path";
import {
  loadConfig,
  resolveSessionId,
  sanitizeSessionKey,
  getSessionKey,
  type CogneePluginConfig,
} from "./plugin-common.ts";

/**
 * Resolve the Cognee session ID for a Vellum conversation.
 *
 * The conversationId from Vellum's hook context is used as the host
 * session key. We map it to a deterministic Cognee session ID of the
 * form `{agentName}_{hostKey}` via first-writer-wins file creation.
 */
export function resolveCogneeSession(conversationId: string): string {
  const cfg = loadConfig();
  const hostKey = sanitizeSessionKey(conversationId);
  return resolveSessionId(hostKey, cfg.agentName);
}

/**
 * Set the COGNEE_SESSION_KEY env var from a conversation ID.
 * This is how hooks pass the session context to the script modules.
 */
export function setSessionEnv(conversationId: string): void {
  const hostKey = sanitizeSessionKey(conversationId);
  if (hostKey) {
    process.env.COGNEE_SESSION_KEY = hostKey;
  }
}

/**
 * Get the plugin root directory. At runtime, this is set by the init
 * hook from ctx.pluginStorageDir or the VELLUM_PLUGIN_ROOT env var.
 */
export function getPluginRoot(): string {
  return process.env.VELLUM_PLUGIN_ROOT ?? dirname(dirname(import.meta.url.replace(/^file:\/\//, "")));
}

/**
 * Get the plugin directory at $VELLUM_WORKSPACE_DIR/plugins/cognee
 * (where the plugin is installed by Vellum's plugin loader).
 */
export function getPluginInstallDir(): string {
  const workspace = process.env.VELLUM_WORKSPACE_DIR;
  if (workspace) return join(workspace, "plugins", "cognee");
  // Fallback: derive from plugin root.
  return getPluginRoot();
}
