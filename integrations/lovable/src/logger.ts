/** Minimal structured logger to stderr. Never log tokens, API keys, or full record content. */
export type LogLevel = "debug" | "info" | "warn" | "error";

const ORDER: Record<LogLevel, number> = { debug: 10, info: 20, warn: 30, error: 40 };

export interface Logger {
  debug(message: string, meta?: Record<string, unknown>): void;
  info(message: string, meta?: Record<string, unknown>): void;
  warn(message: string, meta?: Record<string, unknown>): void;
  error(message: string, meta?: Record<string, unknown>): void;
}

const SECRET_KEYS = /(token|api[-_]?key|authorization|secret|password|cookie)/i;

export function redactMeta(meta: Record<string, unknown> | undefined): Record<string, unknown> | undefined {
  if (!meta) return undefined;
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(meta)) {
    out[key] = SECRET_KEYS.test(key) ? "<redacted>" : value;
  }
  return out;
}

export function createLogger(level: LogLevel = "info", sink: (line: string) => void = (line) => process.stderr.write(`${line}\n`)): Logger {
  const threshold = ORDER[level] ?? ORDER.info;
  const emit = (entryLevel: LogLevel, message: string, meta?: Record<string, unknown>) => {
    if (ORDER[entryLevel] < threshold) return;
    const entry = { time: new Date().toISOString(), level: entryLevel, message, ...(redactMeta(meta) ?? {}) };
    sink(JSON.stringify(entry));
  };
  return {
    debug: (message, meta) => emit("debug", message, meta),
    info: (message, meta) => emit("info", message, meta),
    warn: (message, meta) => emit("warn", message, meta),
    error: (message, meta) => emit("error", message, meta),
  };
}

export const silentLogger: Logger = { debug() {}, info() {}, warn() {}, error() {} };
