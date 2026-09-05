# Cognee for Antigravity

This native Antigravity plugin gives sessions durable Cognee memory. It recalls
relevant context before an invocation, captures prompts and tool results, and persists
completed turns for later retrieval.

## Install

From a checkout of this repository, validate the package before installing it:

```bash
agy plugin validate integrations/antigravity
agy plugin install integrations/antigravity
```

Antigravity CLI 1.1.27 installs the global copy at
`~/.gemini/config/plugins/cognee`; the bundled skills use that location by default.
Set `COGNEE_ANTIGRAVITY_PLUGIN_ROOT` only when the plugin is intentionally installed
somewhere else, including `~/.gemini/antigravity-cli/plugins/cognee` on hosts
using the layout described in the [current CLI documentation](https://antigravity.google/docs/cli/plugins/).

Installation is declarative: it registers the package's `plugin.json` and
`hooks.json` and **never edits Antigravity settings**. It does not write an
Antigravity settings file or settings override.

## Configure Cognee

The plugin shares `~/.cognee/.env` with the other Cognee host plugins. Put either
local-mode LLM credentials or a remote Cognee endpoint there; real shell exports
override the file.

```bash
mkdir -p ~/.cognee
cat >> ~/.cognee/.env <<'EOF'
# Local Cognee
LLM_API_KEY="sk-..."

# Or a remote Cognee server
# COGNEE_BASE_URL="https://your-instance.cognee.ai"
# COGNEE_API_KEY="ck_..."
EOF
chmod 600 ~/.cognee/.env
```

When both local and remote values are configured, the remote endpoint is selected.
For a one-terminal override that affects only Antigravity, set
`COGNEE_ANTIGRAVITY_BACKEND` before launching it:

```bash
export COGNEE_ANTIGRAVITY_BACKEND=local  # or cloud
```

This plugin-specific switch takes precedence over the shared `COGNEE_BACKEND`
switch and does not change the configuration used by other Cognee plugins.

## Data and state boundaries

Private Antigravity hook state—logs, once markers, session maps, pending writes,
and status markers—lives under `~/.cognee-plugin/antigravity/`. Cognee's shared
runtime and data remain in their existing shared locations (`~/.cognee-plugin/`
and `~/.cognee/`), so this plugin does not create a competing runtime.

The adapter maps Antigravity's native `executionId`, `lastUserInput`, `toolCall`,
`result`, `error`, and `finalModelOutput` fields first. It reads at most the final
1 MiB of the JSONL transcript only for enrichment, correlation, and fallback. If
a read begins mid-line, it discards that partial record; malformed or non-object
records are ignored, and the full transcript is never loaded.

## Native hook mapping

`hooks.json` uses Antigravity's named-hook format:

| Named hook | Native event | Cognee action |
| --- | --- | --- |
| `cognee-bootstrap` | `PreInvocation` | Start or connect Cognee for the session |
| `cognee-recall` | `PreInvocation` | Recall relevant context and return it as `injectSteps` |
| `cognee-capture` | `PreInvocation`, `PostToolUse` | Capture the user prompt and matched tool output |
| `cognee-stop` | `Stop` | Store and sync one completed execution without ending the session |

The adapter maps those host events to Cognee's internal `SessionStart`,
`UserPromptSubmit`, `PostToolUse`, and `Stop` contracts. Stop work is deduplicated
per native `executionId`, or per transcript turn and `executionNum` on hosts
using the [documented hook contract](https://antigravity.google/docs/hooks).
Tool retries are deduplicated by step or tool-call identity, and out-of-order
results are paired with the matching tool call. Distinct turns remain separate.
Bootstrap ownership follows the host process, so resuming a conversation after
that process exits starts the runtime again.
Execution sync honors the shared improve cooldown; manual and final sync always
run. The exit watcher remains the sole process/session teardown authority and performs
the final sync and unregister only after Antigravity exits. Hooks are best-effort:
absent or unreadable transcripts do not block native-field capture or Antigravity.

## Shared runtime behavior

Antigravity uses the current Claude Code/Codex runtime behavior: provider extras
are installed in the shared environment, logs and stale session state are bounded,
and configuration comes from shell exports and `~/.cognee/.env`. Legacy
`config.json` values are ignored. A backend without session-aware improve reports
sync as unsupported instead of repeatedly ingesting the full transcript.

The codebase skill uses the current code-graph indexing and search endpoints.
The plugin follows dataset changes recorded for a conversation and includes its
retired sessions in final sync.

## Verify

Re-run the native validator after changing the package:

```bash
agy plugin validate integrations/antigravity
```

It validates the manifest, four bundled skills, and four named hooks without
installing the plugin or changing local Antigravity configuration.

## Capture, events, and project memory

The shared Python hooks support capture opt-out (`COGNEE_CAPTURE=0`), tool/path filtering and credential redaction before buffering or upload. See [the shared controls](../claude-code/README.md#automatic-capture-controls) and [configuration precedence](../CONFIGURATION.md). Structured log migration is documented in [EVENTS.md](EVENTS.md).

Project node sets (`COGNEE_PROJECT_NODE_SET=auto` or a fixed name) and verified companion routing (`COGNEE_SESSION_COMPANION_DATASET=1`) follow the same contract as Claude Code/Codex. They require the server extension in [cognee#4948](https://github.com/topoteretes/cognee/pull/4948); unverified companions fall back to the primary dataset and unsupported project tags keep capture queued. Both features default off.
