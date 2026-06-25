import type { CogneePluginConfig, MemoryScope, ScopeRoute } from "./types.js";

// ---------------------------------------------------------------------------
// Glob matching — uses picomatch-style algorithm (*, **, ?)
//
// Fix #3: Proper glob support instead of naive regex. Handles:
//   *  — any non-separator chars in one segment
//   ** — any depth of segments
//   ?  — single non-separator char
//   [abc] — character class
// ---------------------------------------------------------------------------

/**
 * Convert a glob pattern to a RegExp. Supports *, **, ?, and [charclass].
 */
function globToRegex(pattern: string): RegExp {
  let i = 0;
  let regex = "^";

  while (i < pattern.length) {
    const c = pattern[i];

    if (c === "*") {
      if (pattern[i + 1] === "*") {
        // ** — match any depth
        // Consume optional trailing slash
        if (pattern[i + 2] === "/") {
          regex += "(?:.+/)?";
          i += 3;
        } else {
          regex += ".*";
          i += 2;
        }
      } else {
        // * — match within one segment
        regex += "[^/]*";
        i++;
      }
    } else if (c === "?") {
      regex += "[^/]";
      i++;
    } else if (c === "[") {
      // Character class — pass through until ]
      const start = i;
      i++; // skip [
      if (i < pattern.length && pattern[i] === "!") {
        regex += "[^";
        i++;
      } else {
        regex += "[";
      }
      while (i < pattern.length && pattern[i] !== "]") {
        regex += escapeRegexChar(pattern[i]);
        i++;
      }
      regex += "]";
      i++; // skip ]
    } else if (".+^${}()|\\".includes(c)) {
      regex += "\\" + c;
      i++;
    } else {
      regex += c;
      i++;
    }
  }

  regex += "$";
  return new RegExp(regex);
}

function escapeRegexChar(c: string): string {
  if ("-]\\^".includes(c)) return "\\" + c;
  return c;
}

/**
 * Match a file path against a glob pattern.
 * Paths are normalized to forward slashes.
 */
export function matchGlob(pattern: string, filePath: string): boolean {
  const normalizedPath = filePath.replace(/\\/g, "/");
  const normalizedPattern = pattern.replace(/\\/g, "/");
  return globToRegex(normalizedPattern).test(normalizedPath);
}

/**
 * Route a file to its memory scope based on scopeRouting rules.
 * First matching rule wins; falls back to defaultScope.
 */
export function routeFileToScope(
  filePath: string,
  routes: ScopeRoute[],
  defaultScope: MemoryScope,
): MemoryScope {
  const normalized = filePath.replace(/\\/g, "/");
  for (const route of routes) {
    if (matchGlob(route.pattern, normalized)) {
      return route.scope;
    }
  }
  return defaultScope;
}

/**
 * Determine whether multi-scope mode is active.
 * Active when at least one scope-specific dataset prefix/name/template is configured.
 */
export function isMultiScopeEnabled(cfg: Required<CogneePluginConfig>): boolean {
  return !!(cfg.companyDataset || cfg.userDatasetPrefix || cfg.agentDatasetPrefix || cfg.agentDatasetTemplate);
}

/**
 * Normalize an agent identifier to a stable key.
 *
 * OpenClaw lowercases agent ids at runtime (PluginHookAgentContext.agentId is e.g.
 * "william", not "William"), but config (`agents.list[].id`, `cfg.agentId`) may be
 * mixed-case. Lowercasing everywhere keeps startup seeding (which reads config)
 * and runtime hooks (which read ctx.agentId) pointed at the SAME dataset and
 * the SAME per-agent index bucket — otherwise you get split, e.g. "…-William" vs "…-william".
 */
export function normalizeAgentId(agentId: string | undefined, cfg: Required<CogneePluginConfig>): string {
  return (agentId?.trim() || cfg.agentId || "default").toLowerCase();
}

/**
 * Sanitize a host session id to the same character set the Python integrations use
 * (`_sanitize_session_key`): keep alphanumerics plus `-` `_` `.`, replace anything
 * else with `_`, trim leading/trailing `.`/`_`, and cap length at 120.
 */
function sanitizeSessionKey(value: string): string {
  let safe = "";
  for (const ch of value) safe += /[A-Za-z0-9\-_.]/.test(ch) ? ch : "_";
  return safe.replace(/^[._]+/, "").replace(/[._]+$/, "").slice(0, 120);
}

/**
 * Build the Cognee session id from the host (OpenClaw) session id, following the
 * cross-integration convention `{agent}_{native_session_id}` —
 * e.g. `open_claw_<openclaw-session-id>`. Keeps the session self-describing in the
 * Cognee dashboard and decoupled from working-directory/agent naming. Returns "" for
 * an empty input so downstream truthy checks still skip an absent session id.
 */
export function cogneeSessionId(nativeSessionId: string | undefined): string {
  const native = sanitizeSessionKey((nativeSessionId ?? "").trim());
  return native ? `open_claw_${native}` : "";
}

/**
 * Resolve the Cognee dataset name for a given memory scope.
 *
 * `runtimeAgentId` lets multi-agent gateways pass the active agent's identity
 * (from PluginHookAgentContext.agentId) so the "agent" scope routes to the
 * caller's dataset rather than the static plugin-config agentId. Falls back to
 * cfg.agentId when runtimeAgentId is absent (CLI / background sync paths).
 */
export function datasetNameForScope(
  scope: MemoryScope,
  cfg: Required<CogneePluginConfig>,
  runtimeAgentId?: string,
): string {
  switch (scope) {
    case "company":
      return cfg.companyDataset || `${cfg.datasetName}-company`;
    case "user":
      return cfg.userDatasetPrefix
        ? `${cfg.userDatasetPrefix}-${cfg.userId || "default"}`
        : `${cfg.datasetName}-user-${cfg.userId || "default"}`;
    case "agent": {
      const rawId = runtimeAgentId?.trim() || cfg.agentId || "default";
      // Lowercase ONLY in per-agent mode, so existing single-agent / non-per-agent
      // installs keep their exact dataset names (no orphaning on upgrade).
      // OpenClaw already lowercases the runtime ctx.agentId, so per-agent mode
      // stays internally consistent across startup-seed and runtime hooks.
      const id = cfg.perAgentMemory ? rawId.toLowerCase() : rawId;
      if (cfg.agentDatasetTemplate) {
        return cfg.agentDatasetTemplate.replace(/\{agentId\}/g, id);
      }
      return cfg.agentDatasetPrefix
        ? `${cfg.agentDatasetPrefix}-${id}`
        : `${cfg.datasetName}-agent-${id}`;
    }
  }
}
