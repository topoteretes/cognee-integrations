# Cognee for OpenCode

A native OpenCode memory plugin. Each OpenCode session receives its own
`opencode_` identity, scoped to the canonical workspace. Relevant memories are
recalled on each user turn and added to system context; completed question/answer
pairs and allowed tool traces are captured independently.

## Install

Requires Node 20+ and OpenCode plugin API **1.18.28–1.18.29** (both endpoints are
checked in CI). This narrow range describes the API tested by this package.

```sh
npx @cognee/cognee-opencode setup
export COGNEE_BASE_URL=http://localhost:8011
export COGNEE_API_KEY=your-cognee-api-key
npx @cognee/cognee-opencode health
```

Setup adds this package to the current project's `opencode.json` without storing
credentials. Restart OpenCode after setup. Alternatively install the package in
`.opencode/package.json` and copy `drop-in/cognee.ts` to
`.opencode/plugins/cognee.ts`. Enable one loading method to avoid duplicate hooks.
See [OpenCode plugins](https://opencode.ai/docs/plugins/).

## Configuration

Plugin options may be passed as `{ cognee: { ... } }` or directly. Exported
`COGNEE_BASE_URL`, `COGNEE_API_KEY`, and `COGNEE_PLUGIN_DATASET` override options.
The deprecated `COGNEE_SERVICE_URL` is a lower-priority URL alias. Defaults are
`http://localhost:8011` and `agent_sessions`.

| Option | Default | Purpose |
|---|---|---|
| `autoCapture` | true | Automatic QA and tool capture; `COGNEE_CAPTURE=false` disables it |
| `autoRecall` | true | Per-turn and compaction recall; `COGNEE_RECALL=false` disables it |
| `captureTools` | all | Exact allowlist, or pipe-separated `COGNEE_CAPTURE_TOOLS` |
| `maxCaptureChars` | 8000 | Maximum captured question, answer or tool field after redaction |
| `readScopes` | `{}` | Additional `{user: "user-dataset", company: "company-dataset"}` scopes; agent defaults to the write dataset |
| `recallTimeoutMs` | 2500 | Total prompt recall budget |
| `requestTimeoutMs` | 5000 | Transport timeout for ordinary requests |
| `ingestionTimeoutMs` | 15000 | Capture/write transport timeout |
| `stateDir` | `~/.cognee-plugin/opencode` | Durable outbox and completed-entry IDs |
| `improveOnSessionEnd` | true | Improve a fully flushed session on idle |

Each scope has its own XML label. Backend permissions decide which datasets are
readable. Explicit `cognee_remember` writes permanent facts, and `cognee_search`
works even when automatic recall is disabled. Automatic capture flags do not
disable those user-directed tools or the explicit `index` command.

## Capture and recovery

Automatic capture excludes structured file paths for `.env` variants, SSH/AWS
credential directories, credentials/secrets files, private keys and certificates.
Shell commands are not a file-access sandbox. Common credentials and private-key
blocks are scrubbed recursively **before** truncation and local persistence.
Review your policy before enabling capture for unusual secret formats.

The outbox uses atomic files with owner-only permissions, records native message
and tool-call IDs, and retains failed writes across restarts. Transport timeouts
can happen after a server commit: such entries are held for reconciliation against
session detail instead of blindly submitted twice. The current server exposes only
recent session entries; an ambiguous write absent from that window remains pending
and needs operator reconciliation. `status` reports these as `uncertain`; it never
silently discards them. Explicit authentication/validation rejections retry after
the underlying problem is fixed. A crashed process holding the brief journal lock
can be recovered on the next operation after its PID is confirmed absent.

Registration advertises `type: opencode` and `source: api`. Activity and a 30-second
heartbeat keep sessions alive; idle flushes capture, and plugin disposal unregisters
active sessions. Process termination that bypasses disposal relies on backend
connection expiry. HTTP 404 lifecycle routes are optional; auth failures are logged.
No model/subscription credentials are read from the host application.

## Commands and checks

```sh
npx @cognee/cognee-opencode status
npx @cognee/cognee-opencode index ./notes.txt
npm ci
npm test
npm run typecheck
```

No real LLM or paid backend is required by the tests. They cover request contracts,
per-turn scopes, structured QA/trace capture, lifecycle, exclusions, redaction,
restart recovery, ambiguous commits, and duplicate host events.
