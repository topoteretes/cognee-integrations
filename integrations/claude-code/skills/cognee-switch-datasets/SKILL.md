---
name: cognee-switch-datasets
description: >-
  Switch this session to another Cognee dataset (saves go there, recall restricted to it),
  pin recall to the active one, or set it to auto. Takes a dataset name (created if new),
  "pin" or "auto", or lists them.
when_to_use: >-
  Even without saying Cognee or dataset, e.g. "switch to <dataset>", "use <dataset> for
  this session", "use <dataset> from now on", "only recall from <dataset>", "change my
  dataset", "which datasets can I switch to?", "stop restricting recall", "set recall to
  auto". Not for a one-off search of another dataset (cognee-search), deleting one
  (cognee-forget), git branches or datasets in code.
---

# Switch Datasets

Move this Claude Code session to a different Cognee dataset. A Cognee session
never spans two datasets, so the switch retires the current session (after
syncing it into the graph) and starts a new one on the chosen dataset. `auto`,
`pin` and picking the dataset the session is already on only change the recall
mode: no sync, no new session.

Recall is **auto** by default: graph recall names no dataset, and the server
picks the dataset(s) that best match each prompt, or searches every readable
dataset when it cannot narrow (routing needs a cognee 1.6.6 server; an older
one always searches every readable dataset); saves go to the active dataset.
`COGNEE_PLUGIN_RECALL_MODE=pinned` (exported or in `~/.cognee/.env`) starts
every session with recall pinned to its dataset instead; `COGNEE_PLUGIN_DATASET`
only names the dataset and never pins recall. In a session, switching to
another dataset pins recall to it, `pin` pins it to the active dataset, and
`auto` lifts any pin, a configured one included. `pin` and `auto` leave saves
where they are and start no new session. `auto` is reserved: no dataset can be
named so. `pin` names the action, so a dataset called `pin` is switched to by
its id.

## Faster, with the hooks module loaded

Where Claude Code loads the plugin's hooks module (2.1.287+), the user can switch
without a model turn: `/cognee-dataset <name>` from the prompt (`/cognee-dataset`
alone lists the datasets; `/cognee-dataset pin` and `/cognee-dataset auto` set the
recall mode), or the dataset name in the status row above the prompt,
which is a picker (in a terminal, focus it with `ctrl+x tab`). If the user asks you
to switch, run this skill as below; if they ask
how to switch themselves, point them there.

## Instructions

### 1. If the user only wants to change the recall mode

For "let Cognee pick the dataset again", "stop restricting recall", "set recall
to auto" and the like:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/switch-dataset.py" auto --json
```

For "only recall from the dataset I'm on", "pin recall here", "stop letting
Cognee pick the dataset" and the like (the active dataset stays, no new session):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/switch-dataset.py" pin --json
```

### 2. If the user named a dataset, switch directly

When `$ARGUMENTS` contains a dataset name, skip the picker:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/switch-dataset.py" "$ARGUMENTS" --json
```

### 3. Otherwise, list and let the user choose

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/switch-dataset.py" --list --json
```

The JSON has `current`, `recall` (`"auto"` or `"pinned"`, the mode in effect),
`reason` when pinned (`dataset_picked`, `configured_pin` or
`read_dataset_ids`), `message` (one or two sentences saying which recall is on
and why: relay it as is), `datasets` (`[{name, id, writable, current}]`),
`hidden_readonly` (datasets you can read but not write — never offered) and
`filtered` (`true` when write access was verified for every row).

Present the choice with **AskUserQuestion** (single select), listing the
dataset names from `datasets` (never one named `auto`) with the current one marked "(current)", plus an
"auto (best-matching dataset)" option unless `recall` is already `"auto"`. The
tool allows four options per question, so if there are more than four, put
the first three plus a "More…" option and page on the next question. The
user can always type a name via the built-in "Other" — a name that is not
listed is created for them on switch. If `filtered` is `false`, say so in the
question text ("write access could not be verified for these").

Then run the switch with the chosen name (`auto` for the auto option, `pin`
when the user picked the current dataset):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/switch-dataset.py" "<chosen name>" --json
```

### 4. Report the result

Every successful result carries `message`: one or two plain sentences saying
what changed and which recall is now in effect. **Relay `message` to the user
as is**; do not reword it from `reason`. The status line updates within a
couple of seconds.

A switch is `{"switched": true, "dataset", "session_id", "previous": {"dataset",
"session_id", "synced": true|false}, "message"}`. `"switched": false` (`pin`,
`auto`, or the current dataset named) carries a `reason` for logic only:
`recall_restricted`, `already_active`, `recall_auto` or `already_auto`, and
`recall`, the mode now in effect (`"pinned"` after `auto` means
`COGNEE_PLUGIN_READ_DATASET_IDS` still scopes recall; `message` says so).

On failure the JSON is `{"error", "code"}`:

| code | meaning | what to do |
|------|---------|------------|
| 2 | launch record not found | the plugin did not initialise this session; run `"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-doctor.sh"`. If several Claude Code sessions share this directory, rerun with `--session-key <host session id>` |
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

- A switch is not needed just to *look* in another dataset: a one-off graph search
  leaves the session where it is — `"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "<query>" 10 --graph --dataset-id <id> --user-picked`, for a dataset the user named or picked (see the **cognee-search** skill).

- `COGNEE_PLUGIN_DATASET` seeds the dataset at launch; a switch overrides it for the rest of the session and survives `--resume`. It never restricts recall. `COGNEE_PLUGIN_DATASET=auto` is not a dataset name: it means the default dataset (`agent_sessions`).
- `COGNEE_PLUGIN_RECALL_MODE` (`auto`, the default, or `pinned`; exported or in `~/.cognee/.env`) is the recall mode a session starts in; an unknown value means `auto`. `pin`, `auto` or a switch in the session wins over it for that session.
- After a switch, recall is restricted to the new dataset, so context from the previous one is no longer injected. `switch-dataset.py auto` puts recall back on auto, which lets the server pick the best-matching readable dataset(s) again.
