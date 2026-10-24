// Bundles dist/worker.js and dist/manifest.js the way the Paperclip host
// loads them (ESM, node24, SDK externalised for the manifest), using the
// SDK's own presets so the host contract stays in one place.
import { build } from "esbuild";
import { createPluginBundlerPresets } from "@paperclipai/plugin-sdk/bundlers";

const presets = createPluginBundlerPresets({ workerEntry: "src/worker.ts", manifestEntry: "src/manifest.ts", outdir: "dist" });
await build({ ...presets.esbuild.worker, logLevel: "warning" });
await build({ ...presets.esbuild.manifest, logLevel: "warning" });
