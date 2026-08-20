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

Antigravity installs the global copy at
`~/.gemini/config/plugins/cognee`; the bundled skills use that location by default.
Set `COGNEE_ANTIGRAVITY_PLUGIN_ROOT` only when the plugin is intentionally installed
somewhere else.

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

The adapter reads at most the final 1 MiB of an Antigravity JSONL transcript. If a
read begins mid-line, it discards that partial record; malformed or non-object
records are ignored. It derives the latest explicit user prompt, a matching tool
result, or the completed assistant response only from that bounded tail—never by
loading the full transcript.

## Native hook mapping

`hooks.json` uses Antigravity's named-hook format:

| Named hook | Native event | Cognee action |
| --- | --- | --- |
| `cognee-bootstrap` | `PreInvocation` | Start or connect Cognee for the session |
| `cognee-recall` | `PreInvocation` | Recall relevant context and return it as `injectSteps` |
| `cognee-capture` | `PreInvocation`, `PostToolUse` | Capture the user prompt and matched tool output |
| `cognee-stop` | `Stop` | Store the completed turn and request session-to-graph sync |

The adapter maps those host events to Cognee's internal `SessionStart`,
`UserPromptSubmit`, `PostToolUse`, `Stop`, and `SessionEnd` contracts. Hooks are
best-effort: absent or unreadable transcripts produce no captured records rather
than blocking Antigravity.

## Verify

Re-run the native validator after changing the package:

```bash
agy plugin validate integrations/antigravity
```

It validates the manifest, four bundled skills, and four named hooks without
installing the plugin or changing local Antigravity configuration.
