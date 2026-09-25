---
name: setup
description: Use when configuring the Cognee connection, checking whether Cognee is ready or connected, or diagnosing a local or cloud Cognee instance from Codex.
---

# Cognee Connection And Status

Use this skill when the user asks to configure Cognee, check whether Cognee is
ready or connected, switch between local and cloud mode, or diagnose a
connection problem.

## Rules

- **Server first.** Answer every question here from the plugin's own diagnostic
  and the running Cognee server over HTTP. That is the authoritative path in
  both local and cloud mode.
- **`cognee-cli` is a last resort, not an alternative.** It is reachable only on
  a machine holding a cognee **source checkout**: `scripts/cognee-cli.sh` exits
  64 ("Run this from the Cognee repository root or set COGNEE_REPO_ROOT")
  anywhere else, and in cloud mode the plugin's venv is never built at all. Never
  reach for it before the two steps below have actually failed.
- **Never run `uv sync` or any dependency install.** On a plugin install there is
  no cognee repository to sync, and in the user's own project it would rebuild
  their dependencies.
- Do not print secret values from `.env`, config files, shell history, or command
  output. Report whether a key *appears configured*, never the value.
- If a command may create, delete, or overwrite durable Cognee state, say what it
  will affect before running it.
- Do not use MCP for this plugin.

## 1. Check the mode and the connection

One command answers "is Cognee ready?", "which mode am I in?", and "what is
wrong?" — it imports no cognee, makes one `/health` probe, and works from any
directory:

```bash
python3 "${CODEX_PLUGIN_ROOT}/scripts/doctor.py" --json
```

The JSON carries:

| Field | Meaning |
|-------|---------|
| `mode` | `Local` or `Cloud`, plus an annotation naming what forced the decision (e.g. `forced by COGNEE_BACKEND=local`) |
| `server_url` | The endpoint this terminal actually resolved |
| `reachable` | Whether the health probe succeeded |
| `latency_ms` | Round-trip time, or `null` when unreachable |
| `api_key_source` | Which credential the plugin is using (plugin identity, env var, cached, minted) |
| `memory_sharing` | Shared-agent-memory state |
| `circuit_breaker` | Whether recall is currently short-circuited |

Read `mode` **before** doing anything mode-specific. In particular: in `Cloud`
mode there is no local server to start and no local UI to launch — see the
**local-ui** skill, which stops on cloud by design.

Drop `--json` for a human-readable table when reporting to the user.

## 2. Probe the server directly (optional)

When you need to confirm the endpoint yourself, or the doctor's verdict looks
stale:

```bash
curl -sS -i "${COGNEE_BASE_URL:-http://localhost:8011}/health"
```

`http://localhost:8011` is the plugin's local server. Do **not** substitute
`:8000` — that is the `cognee-cli`'s own default for a server it would start
itself, unrelated to the one the plugin runs and talks to.

For an authenticated check of the resolved credential (cloud, or any server with
auth enabled), use the forget helper's credential resolver rather than guessing
where the key lives:

```bash
eval "$(${CODEX_PLUGIN_ROOT}/scripts/cognee-forget.sh env)" && \
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "X-Api-Key: ${COGNEE_API_KEY}" "${COGNEE_BASE_URL}/api/v1/users/me"
```

`200` means the key is accepted; `401`/`403` means it was rejected by that
server. In local mode the key is never exported to the shell, so a raw request
without this resolver will 401 misleadingly.

## Configure the connection

Durable configuration (`COGNEE_BASE_URL`, `COGNEE_API_KEY`, `LLM_API_KEY`, ...)
belongs in `~/.cognee/.env` — a one-time setup file shared with the Claude Code
plugin, loaded at session start. Guide the user to edit that file rather than
exporting in every shell; never echo its secret values. Shell exports still
override it per terminal. Changes apply on the next `codex` launch.

**Cloud or a remote server** — both values:

```bash
mkdir -p ~/.cognee
cat >> ~/.cognee/.env <<'EOF'
COGNEE_BASE_URL="https://your-instance.cognee.ai"
COGNEE_API_KEY="ck_..."
EOF
chmod 600 ~/.cognee/.env
```

**Local** (default when `COGNEE_BASE_URL` is unset) — the plugin bootstraps a
local Cognee API on `http://localhost:8011` and auto-mints `COGNEE_API_KEY`, so
only the LLM key is required:

```bash
mkdir -p ~/.cognee
cat >> ~/.cognee/.env <<'EOF'
LLM_API_KEY="sk-..."
EOF
chmod 600 ~/.cognee/.env
```

Re-pasting either block is safe — the last value of a repeated key wins.

### Which mode wins

The file may hold **both** modes' variables at once. The mode is then decided
per terminal, in this order:

1. An exported `COGNEE_BACKEND` (or the plugin-specific `COGNEE_CODEX_BACKEND`,
   which beats it) pins that terminal: `local` or `cloud`.
2. Otherwise cloud wins when `COGNEE_BASE_URL` is set anywhere.
3. Otherwise local.

A forced mode is **pinned**: `COGNEE_BACKEND=cloud` with no URL configured stays
cloud and fails visibly (`✕ (missing_cognee_base_url)`) rather than silently
falling back. `unset COGNEE_BASE_URL` is **not** a way to go local — the env file
re-injects it on the next launch; use the switch.

`COGNEE_BACKEND` may also live in `~/.cognee/.env` to make a mode the durable
default. The doctor's `mode` field always reports what the terminal actually
resolved, with the cause.

## Fallback — `cognee-cli`, local mode with a cognee checkout only

Only if steps 1 and 2 both failed, the mode is `Local`, **and** the machine has a
cognee source checkout. Route it through the wrapper so a missing checkout fails
with a clear message instead of an opaque `uv` error:

```bash
"${CODEX_PLUGIN_ROOT}/scripts/cognee-cli.sh" config list
```

Set `COGNEE_REPO_ROOT` to the checkout path if the wrapper cannot find it. Use
`config get <KEY>` only for non-secret settings.

Exit code 64 means there is no cognee checkout here — that is expected on a
normal install, and it is **not** a Cognee fault to report. Say the server is
unreachable and give the user the doctor output instead.
