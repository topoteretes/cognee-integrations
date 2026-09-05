---
name: cognee-switch-datasets
description: Switch this session to another Cognee dataset. Lists the datasets you can write to, lets you pick one, syncs the current session into its dataset, then starts a fresh Cognee session bound to the chosen dataset. All later saves and recalls target the new dataset and the status line shows it.
---

# Switch Datasets

Move this Claude Code session to a different Cognee dataset. A Cognee session
never spans two datasets, so the switch retires the current session (after
syncing it into the graph) and starts a new one on the chosen dataset.

## Instructions

### 1. If the user named a dataset, switch directly

When `$ARGUMENTS` contains a dataset name, skip the picker:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/switch-dataset.py "$ARGUMENTS" --json
```

### 2. Otherwise, list and let the user choose

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/switch-dataset.py --list --json
```

The JSON has `current`, `datasets` (`[{name, id, writable, current}]`),
`hidden_readonly` (datasets you can read but not write — never offered) and
`filtered` (`true` when write access was verified for every row).

Present the choice with **AskUserQuestion** (single select), listing the
dataset names from `datasets` with the current one marked "(current)". The
tool allows four options per question, so if there are more than four, put
the first three plus a "More…" option and page on the next question. The
user can always type a name via the built-in "Other" — a name that is not
listed is created for them on switch. If `filtered` is `false`, say so in the
question text ("write access could not be verified for these").

Then run the switch with the chosen name:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/switch-dataset.py "<chosen name>" --json
```

### 3. Report the result

On success the JSON is
`{"switched": true, "dataset", "session_id", "previous": {"dataset", "session_id", "synced": true|false}}`.
Tell the user in one line which dataset and Cognee session are now active and
that the previous session was synced. The status line updates within a couple
of seconds. If `"switched": false` with `"reason": "already_active"`, say the
session is already on that dataset.

On failure the JSON is `{"error", "code"}`:

| code | meaning | what to do |
|------|---------|------------|
| 2 | launch record not found | the plugin did not initialise this session; run `/cognee-memory:cognee-doctor` |
| 3 | syncing the current session failed | nothing was changed; show the error. Re-run with `--force` **only if the user explicitly accepts** that unsynced entries of the current session are retried at session end instead |
| 4 | registering the new session failed | nothing was changed; the server rejected the registration — check the connection |
| 5 | dataset not writable | the name is readable but owned by another principal; pick from `--list` |

## What the switch does

1. Syncs the current session into its dataset (`sync-session-to-graph.py --strict`) — aborts if this fails.
2. Ensures the target dataset exists for this principal.
3. Registers a **new** Cognee session on the target dataset under a fresh connection handle, then releases the old handle (register-then-unregister, so a local agent-mode server never sees zero connections).
4. Repoints this launch's record: every hook, the shell wrappers, the idle/exit watchers and the status line follow it.
5. Retired sessions stay in the record's `touched` list; the session-end sync covers them again as a safety net.

## Notes

- `COGNEE_PLUGIN_DATASET` only seeds the dataset at launch; a switch overrides it for the rest of the session and survives `--resume`.
- Recall is scoped to the active dataset, so after a switch earlier context from the previous dataset is no longer injected — switch back to see it again.
