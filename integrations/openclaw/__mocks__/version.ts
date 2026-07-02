// ---------------------------------------------------------------------------
// Test stub for src/version.ts
//
// Returned by the Jest moduleNameMapper when tests import ./version (or any
// path ending in /version). This avoids ts-jest encountering import.meta.url,
// which is a SyntaxError in the CJS context Jest uses even with useESM:true.
//
// The values are read from the real package.json via plain require(), which
// is available in Jest's CJS runtime without any special configuration.
// ---------------------------------------------------------------------------

// eslint-disable-next-line @typescript-eslint/no-require-imports
const _pkg = require("../package.json") as { name: string; version: string };

export const PLUGIN_NAME: string = _pkg.name;
export const PLUGIN_VERSION: string = _pkg.version;
