// ---------------------------------------------------------------------------
// Plugin version — read from package.json once at module-load time.
//
// Using createRequire(import.meta.url) is the idiomatic way to load JSON in
// an ESM-first project without toggling resolveJsonModule in tsconfig. The
// result is computed once and re-used by every caller; there is no run-time
// overhead beyond a single synchronous file read at module initialisation.
//
// Note for contributors: the Jest test runner uses a moduleNameMapper entry
// in package.json that redirects imports of this module to
// __mocks__/version.ts, avoiding the import.meta SyntaxError that occurs
// when ts-jest compiles ESM source to CJS for its test VM context.
// ---------------------------------------------------------------------------

import { createRequire } from "node:module";

const _require = createRequire(import.meta.url);
const _pkg = _require("../package.json") as { name: string; version: string };

/** Installed plugin name (e.g. "@cognee/cognee-openclaw"). */
export const PLUGIN_NAME: string = _pkg.name;

/** Installed plugin version string (e.g. "2026.6.11"). */
export const PLUGIN_VERSION: string = _pkg.version;
