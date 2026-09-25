# Cognee for Antigravity

This native Antigravity plugin gives sessions durable Cognee memory. It recalls
relevant context before an invocation, captures prompts and tool results, and persists
completed turns for later retrieval.

## Install

**Requirements.** Any Python 3.9 or newer available as `python3` (or `python`) on PATH — the hooks are stdlib-only HTTP clients and never import cognee, so the Python 3.9.6 that ships with macOS's Xcode Command Line Tools is enough. In local mode the plugin brings its own runtime for the Cognee server: it fetches [uv](https://docs.astral.sh/uv/) into `~/.cognee-plugin/uv` and builds a Python 3.12 virtualenv with it (reusing a 3.12 already on the machine, otherwise downloading a ~66 MB standalone build). Only when uv is absent *and* cannot be downloaded does the plugin fall back to the host interpreter, and that fallback needs Python 3.10 or newer: on an older host it refuses, logs `host_python_too_old_for_venv` to `hook.log`, and every session start says so until uv or a newer python3 is installed. Cloud mode never builds a runtime. (SDK-based integrations such as LangGraph or CrewAI import cognee in-process and need Python 3.10+; see [`CONFIGURATION.md`](../CONFIGURATION.md#python-version-requirements).)

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

**Default user and its password.** The local server is started with `DEFAULT_USER_EMAIL=default_user@example.com` and `DEFAULT_USER_PASSWORD=default_password`, which is how cognee 1.6.0 and later create the default user at all (a server started without `DEFAULT_USER_PASSWORD` creates no default account, and the password is set once and never rewritten). The plugin logs in as that user to mint its owner API key, so a fresh install needs no manual step and an existing install keeps working. Exporting `DEFAULT_USER_EMAIL`/`DEFAULT_USER_PASSWORD` yourself overrides what the plugin passes; `COGNEE_USER_EMAIL`/`COGNEE_USER_PASSWORD` pick the user the plugin logs in as, and a non-default user must already exist on the server. When pointing at a server you run yourself (`COGNEE_BASE_URL`), either start it with `DEFAULT_USER_PASSWORD` set to the same value as `COGNEE_USER_PASSWORD`, or set `COGNEE_API_KEY` so no login is needed; a server without either answers the login with an error that says so.

## Plugin identity and shared agent memory

Antigravity follows the same identity model as the Claude Code and Codex plugins:

- `COGNEE_PLUGIN_IDENTITY` — `auto` (default) provisions a dedicated agent identity only
  in service of shared agent memory and falls back to your principal key when that
  cannot be wired; `true` requires an identity (create-only, never rotating a key
  another machine holds) and never falls back to the owner; `false` runs as the
  principal. The server's plugin registry must list `antigravity` for provisioning to
  succeed; until it does, `auto` stays on the principal.
- `COGNEE_SHARED_AGENT_MEMORY` — on by default: every plugin agent of your user joins
  one `cognee-agent` role with read+write on your datasets, and the launch's dataset is
  addressed by its canonical UUID, so Antigravity recalls what Claude Code and Codex
  stored and vice versa. `false` gives separated, per-plugin memory (the agent leaves
  the shared role). See the Claude Code plugin README for the full description.
- `COGNEE_PLUGIN_READ_DATASET_IDS` — a JSON array of dataset UUIDs for federated graph
  recall; session history stays scoped to its own dataset.

The doctor shows the effective state under **API Key Source** and **Memory Sharing**.

#### How the two settings combine

`COGNEE_PLUGIN_IDENTITY` decides *who the plugin authenticates as, and how strictly*;
`COGNEE_SHARED_AGENT_MEMORY` decides *what an agent identity can see*. Sharing is a
property of agent identities — your own user sees everything regardless — so the second
setting only matters once an identity exists.

| `COGNEE_PLUGIN_IDENTITY` \ `COGNEE_SHARED_AGENT_MEMORY` | `true` (default) | `false` |
|---|---|---|
| `auto` (default) | **Shared memory, graceful.** Provisions an identity when the server allows it, wires the shared role, and falls back to your principal key whenever that cannot be done. | **Principal, unless already provisioned.** A fresh install never provisions (`auto` provisions only in service of sharing). An identity provisioned earlier is kept, leaves the shared role, and writes to its own private dataset. |
| `true` | **Shared memory, strict.** Same wiring; any obstacle (no `create_only` support, a credential bound to another principal, a rejected key) is an error — never a silent fall back to the owner's key. | **Separated identities, strict.** Each plugin is its own agent with its own private memory, blind to your other datasets. This is the isolation mode: a leaked or revoked plugin key affects only that plugin. |
| `false` | **Principal only.** The sharing setting has no effect. | **Principal only.** Identical to the cell above. |

Practical reading: leave both at their defaults for one memory across all of your plugins;
set `COGNEE_PLUGIN_IDENTITY=true` when you want the strict guarantees; add
`COGNEE_SHARED_AGENT_MEMORY=false` to that for fully separated per-plugin memory. Setting
`COGNEE_PLUGIN_IDENTITY=false` makes the sharing setting irrelevant. The doctor reports the
resulting state under **API Key Source** and **Memory Sharing**.

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
