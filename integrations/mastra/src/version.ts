/**
 * Package version, kept in sync with `package.json` by hand (no codegen
 * step in this package). This is a plain literal rather than a
 * `require("../package.json")` read because the build target is ESM/NodeNext
 * without `resolveJsonModule` wired in, and a literal keeps `dist/` free of
 * a runtime dependency on the source tree's `package.json` file existing
 * next to it after publish.
 *
 * `CHANGELOG.md`'s latest entry must match this value; nothing enforces
 * that automatically — bump both by hand on release.
 */
export const VERSION = "0.1.0";
