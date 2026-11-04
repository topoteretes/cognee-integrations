/**
 * A `runPluginCommandWithTimeout` that really runs the command.
 *
 * Every other tier maps `openclaw/plugin-sdk/sandbox` to `__mocks__/openclaw-sandbox.cjs`
 * (see `moduleNameMapper` in package.json), whose implementation returns
 * `{ code: 0 }` without spawning anything. Right for hermetic tests — the plugin's
 * boot path must not install a venv on a developer's machine because a unit test
 * ran. Fatal for the live tier when it boots a server: `bootServerIfNeeded` wrote
 * `ensure_and_boot.py`, "ran" it, saw exit 0, and then waited 600s for a server
 * that no process was ever going to start. The first self-booting run failed
 * exactly like that, with no warning to point at the cause.
 *
 * The real SDK module is ESM and cannot be `requireActual`'d under ts-jest's
 * CommonJS transform, so the live test swaps this in with `jest.mock`. It keeps
 * the SDK's contract for the one call the plugin makes — argv, optional env/cwd,
 * a timeout, `{ code, stdout, stderr }` back — and nothing else.
 */

import { execFile } from "node:child_process";

export interface RunPluginCommandOptions {
  argv: string[];
  timeoutMs: number;
  cwd?: string;
  env?: NodeJS.ProcessEnv;
}

export interface RunPluginCommandResult {
  code: number;
  stdout: string;
  stderr: string;
}

export async function runPluginCommandWithTimeout(
  options: RunPluginCommandOptions,
): Promise<RunPluginCommandResult> {
  const [command, ...args] = options.argv;
  if (!command) return { code: 1, stdout: "", stderr: "command is required" };
  return new Promise((resolve) => {
    execFile(
      command,
      args,
      { cwd: options.cwd, env: options.env ?? process.env, timeout: options.timeoutMs },
      (error, stdout, stderr) => {
        const code =
          error && typeof (error as NodeJS.ErrnoException & { code?: unknown }).code === "number"
            ? ((error as { code: number }).code as number)
            : error
              ? 1
              : 0;
        resolve({
          code,
          stdout: String(stdout ?? ""),
          stderr: error && !stderr ? String(error.message) : String(stderr ?? ""),
        });
      },
    );
  });
}

export function resolvePreferredOpenClawTmpDir(): string {
  return "/tmp/openclaw";
}
