#!/usr/bin/env node
/**
 * `lovable-memory` — operator commands. Configuration comes from the
 * environment (see README); nothing here is reachable from Lovable.
 *
 *   serve                 start the connector
 *   health                check the backend, datasets and ledger; exit 1 on problems
 *   token                 print a fresh connector token for LOVABLE_MCP_TOKEN
 *   receipts [status]     list receipts (optionally one status)
 *   status <receipt-id>   refresh and print one receipt
 *   reconcile             re-check every pending receipt against the backend
 *   forget <receipt-id> --yes   delete one record on the backend, verify, clear locally
 *   config                print the resolved configuration with secrets masked
 */
import { generateToken } from "./auth.js";
import { redactConfig, resolveConfig } from "./config.js";
import { ConfigError } from "./errors.js";
import { createLogger } from "./logger.js";
import { MemoryService } from "./memory.js";
import { createConnectorServer } from "./server.js";
import { PACKAGE_NAME, PACKAGE_VERSION } from "./version.js";

const USAGE = `${PACKAGE_NAME} ${PACKAGE_VERSION}

Usage: lovable-memory <command> [args]

  serve                      start the MCP connector (POST \${LOVABLE_MCP_PATH:-/mcp})
  health                     backend, datasets and receipt ledger check
  token                      generate a connector token for LOVABLE_MCP_TOKEN
  receipts [status]          list receipts; status = accepted|indexing|ready|failed|unknown|deleted
  status <receipt-id>        refresh one receipt against the backend
  reconcile                  refresh every pending receipt
  forget <receipt-id> --yes  delete one stored record (verified) and clear it locally
  config                     print resolved configuration (secrets masked)
`;

function print(value: unknown): void {
  process.stdout.write(`${typeof value === "string" ? value : JSON.stringify(value, null, 2)}\n`);
}

async function main(argv: string[]): Promise<number> {
  const [command, ...rest] = argv;
  if (!command || command === "help" || command === "--help" || command === "-h") {
    print(USAGE);
    return 0;
  }
  if (command === "token") {
    print(generateToken());
    return 0;
  }

  let config;
  try {
    config = resolveConfig();
  } catch (error) {
    if (error instanceof ConfigError) {
      process.stderr.write(`configuration error:\n${error.message}\n`);
      return 2;
    }
    throw error;
  }
  const logger = createLogger(config.logLevel);

  if (command === "config") {
    print(redactConfig(config));
    return 0;
  }

  const service = new MemoryService({ config, logger });
  try {
    switch (command) {
      case "serve": {
        const report = await service.prepare();
        for (const problem of report.problems) logger.warn("startup", { problem });
        if (!report.backendReachable) {
          logger.error("cognee backend is not reachable; the connector will answer tool calls with 'unavailable' until it is", { baseUrl: config.cognee.baseUrl });
        }
        const connector = createConnectorServer({ config, service, logger });
        const bound = await connector.listen();
        logger.info("listening", {
          url: bound.url,
          publicUrl: config.http.publicUrl ?? null,
          allowedHosts: config.http.allowedHosts,
          projectDataset: config.projectDataset,
          sharedDatasets: config.sharedDatasets,
          receipts: config.receipts.path,
        });
        await new Promise<void>((resolve) => {
          const stop = () => {
            logger.info("shutting down");
            void connector.close().then(resolve);
          };
          process.once("SIGINT", stop);
          process.once("SIGTERM", stop);
        });
        return 0;
      }
      case "health": {
        const report = await service.health();
        print(report);
        return report.backendReachable && report.problems.length === 0 ? 0 : 1;
      }
      case "receipts": {
        const status = rest[0] as never;
        print(service.store.list(status ? { status } : {}).map(summarize));
        return 0;
      }
      case "status": {
        if (!rest[0]) {
          process.stderr.write("usage: lovable-memory status <receipt-id>\n");
          return 2;
        }
        print(await service.status(rest[0]));
        return 0;
      }
      case "reconcile": {
        print(await service.reconcile());
        return 0;
      }
      case "forget": {
        if (!rest[0]) {
          process.stderr.write("usage: lovable-memory forget <receipt-id> --yes\n");
          return 2;
        }
        if (!rest.includes("--yes")) {
          const receipt = service.store.get(rest[0]);
          print(receipt ? summarize(receipt) : { receiptId: rest[0], status: "not_found" });
          process.stderr.write("re-run with --yes to delete this record on the backend\n");
          return 1;
        }
        const result = await service.forget(rest[0]);
        print(result);
        return result.verified ? 0 : 1;
      }
      default:
        process.stderr.write(`unknown command: ${command}\n${USAGE}`);
        return 2;
    }
  } finally {
    service.close();
  }
}

function summarize(receipt: { id: string; status: string; dataset: string; dataId: string | null; createdAt: string; updatedAt: string; error: string | null; content: string | null; sourceRef: string | null }) {
  return {
    id: receipt.id,
    status: receipt.status,
    dataset: receipt.dataset,
    dataId: receipt.dataId,
    createdAt: receipt.createdAt,
    updatedAt: receipt.updatedAt,
    error: receipt.error,
    sourceRef: receipt.sourceRef,
    preview: receipt.content ? `${receipt.content.slice(0, 120)}${receipt.content.length > 120 ? "…" : ""}` : null,
  };
}

main(process.argv.slice(2)).then(
  (code) => process.exit(code),
  (error) => {
    process.stderr.write(`${(error as Error)?.stack ?? String(error)}\n`);
    process.exit(1);
  },
);
