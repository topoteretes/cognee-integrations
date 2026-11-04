/**
 * Per-machine API keys of cognee plugin agents (cognee#6113).
 *
 * Every machine a cognee plugin (Claude Code, Codex, ...) runs on holds its own
 * key for that plugin's agent; the server caps them. A full agent refuses a
 * new machine a key, which then runs on the user's personal key until an old
 * machine's key is revoked — `openclaw cognee keys` is how a user does that
 * from here. Kept apart from the HTTP client so the CLI tests, which mock the
 * client module wholesale, still exercise the real ordering.
 */

/** One per-machine key of a plugin agent, as `openclaw cognee keys` lists it. */
export interface CogneePluginKey {
  plugin: string;
  id: string;
  label: string;
  createdAt: string;
  /** Empty when the key was never used. */
  lastUsedAt: string;
}

/** Most recently used first; never-used keys last, newest of those first. */
export function sortPluginKeysByLastUse(keys: CogneePluginKey[]): CogneePluginKey[] {
  return [...keys].sort((a, b) => {
    const ka = a.lastUsedAt ? `1${a.lastUsedAt}` : `0${a.createdAt}`;
    const kb = b.lastUsedAt ? `1${b.lastUsedAt}` : `0${b.createdAt}`;
    return kb.localeCompare(ka);
  });
}
