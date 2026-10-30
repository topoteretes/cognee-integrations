---
name: cognee-keys
description: Manage the API keys of this plugin's Cognee agent, one per machine. Use when the user asks to list, clean up or revoke Cognee plugin keys, when a session start says this machine is on the personal key because the agent is full, or when "cognee keys" / "key limit" / "revoke a key" comes up.
---

# Cognee Keys

> **Plugin root.** The commands below use `${CURSOR_PLUGIN_ROOT}`, which Cursor
> does not set in your shell. Take the path from the `Cognee plugin root:` line
> in the session context (or use the directory two levels above this `SKILL.md`,
> the one holding `scripts/`) and run `export CURSOR_PLUGIN_ROOT="<that path>"`
> in the same shell command, before the skill command.

Every machine this plugin runs on holds its own API key for the plugin's Cognee agent.
The server caps how many keys an agent may hold; when it is full of keys still in use, a
new machine is refused one and runs on the user's personal key instead (session start
says so). Freeing a slot means revoking an old machine's key — a key that is still in
use on another machine stops working there, so the user chooses which one.

All server access goes through `plugin-keys.py`, which authenticates as the user
principal (the agent cannot manage its own keys) the same way the other skills resolve
credentials. Do not bypass it with raw `curl`.

## Instructions

### 1. List the keys, most recently used first

```bash
python3 "${CURSOR_PLUGIN_ROOT}/scripts/plugin-keys.py" list --json
```

Each key has `id`, `created_at`, `last_used_at` (empty when never used) and
`this_machine` (true for the key this machine holds). Show the user the list as a numbered
table — last used, created, and a short id — and mark this machine's key. Keys never used
sort last. `--all` lists every plugin agent of the user (Claude Code, Codex, …) with a
`plugin` column; `--plugin <key>` lists one other plugin's.

Exit code 5 means the server has no key routes for this plugin (it predates per-machine
keys, or does not register the plugin); say so and stop.

### 2. Let the user choose, then revoke

Never pick a key for the user: the one with the oldest `last_used_at` is the usual
candidate (an old laptop, a wiped container), but only the user knows which machines are
still theirs. Confirm the choice, then:

```bash
python3 "${CURSOR_PLUGIN_ROOT}/scripts/plugin-keys.py" revoke <id> --json
```

Add `--plugin <key>` for another plugin's key. Revoking the key marked `this_machine`
is refused unless the user explicitly wants it (`--force`); the next session start then
provisions a new one. Exit code 3 means the key was already gone.

### 3. Tell the user what happens next

If the session start had reported the key limit, a new session now gets its own key.
Revocation is immediate: the revoked machine falls back to the personal key at its next
session start.
