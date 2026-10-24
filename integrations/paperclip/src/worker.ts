/**
 * Worker entrypoint: the host runs `node dist/worker.js` and calls `setup(ctx)`.
 * When this module is merely imported (tests), `runWorker`'s main-module check
 * keeps the RPC host from starting.
 */
import { runWorker } from "@paperclipai/plugin-sdk";
import { createPlugin } from "./plugin.js";

const { plugin } = createPlugin();

export default plugin;
runWorker(plugin, import.meta.url);
