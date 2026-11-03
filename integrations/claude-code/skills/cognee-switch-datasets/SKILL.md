---
name: cognee-switch-datasets
description: >-
  Switch this session to another Cognee dataset (saves go there, recall is restricted to
  it), or set recall back to auto. Takes a dataset name (created if new) or "auto", or
  lists writable datasets.
when_to_use: >-
  Even without saying Cognee or dataset, e.g. "switch to <dataset>", "use <dataset> for
  this session", "use <dataset> from now on", "only recall from <dataset>", "change my
  dataset", "which datasets can I switch to?", "recall from all datasets again", "set
  recall to auto". Not for a one-off search of another dataset (cognee-search), deleting
  one (cognee-forget), git branches or datasets in code.
---

# Switch Datasets

Move this Claude Code session to a different Cognee dataset. A Cognee session
never spans two datasets, so the switch retires the current session (after
syncing it into the graph) and starts a new one on the chosen dataset.

Recall is **auto** by default: a cognee >= 1.6.6 server picks the dataset(s)
that best match each prompt when it can, and otherwise searches every readable
dataset; saves go to the active dataset. Picking a dataset — another one, or
the active one — restricts recall to it. `auto` lifts the restriction and leaves
saves where they are. `auto` is reserved: no dataset can be named so.

## Faster, with the hooks module loaded

Where Claude Code loads the plugin's hooks module (2.1.287+), the user can switch
without a model turn: `/cognee-dataset <name>` from the prompt (`/cognee-dataset`
alone lists the datasets), or the dataset name in the status row above the prompt,
which is a picker (in a terminal, focus it with `ctrl+x tab`). If the user asks you
to switch, run this skill as below; if they ask
how to switch themselves, point them there.

## Instructions

### 1. If the user wants recall back on auto

For "recall from all datasets again", "set recall to auto" and the like:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/switch-dataset.py" auto --json
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

The JSON has `current`, `recall` (`"auto"` or `"pinned"`, the mode in effect:
an older server that cannot route reads as `"pinned"`), `datasets`
(`[{name, id, writable, current}]`), `hidden_readonly` (datasets you can read but not write — never offered) and
`filtered` (`true` when write access was verified for every row).

Present the choice with **AskUserQuestion** (single select), listing the
dataset names from `datasets` (never one named `auto`) with the current one marked "(current)", plus an
"auto (best-matching dataset)" option unless `recall` is already `"auto"`. The
tool allows four options per question, so if there are more than four, put
the first three plus a "More…" option and page on the next question. The
user can always type a name via the built-in "Other" — a name that is not
listed is created for them on switch. If `filtered` is `false`, say so in the
question text ("write access could not be verified for these").

Then run the switch with the chosen name (`auto` for the auto option):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/switch-dataset.py" "<chosen name>" --json
```

### 4. Report the result

On success the JSON is
`{"switched": true, "dataset", "session_id", "previous": {"dataset", "session_id", "synced": true|false}}`.
Tell the user in one line which dataset and Cognee session are now active and
that the previous session was synced. The status line updates within a couple
of seconds. `"switched": false` carries a `reason`:

| reason | say |
|--------|-----|
| `already_active` | the session is already on that dataset, recall restricted to it |
| `recall_restricted` | recall is now restricted to that (already active) dataset |
| `recall_auto` | recall is auto again; saves still go to `dataset` (if `recall` is `"pinned"`: saved, but this server can't route yet, so recall stays on `dataset`) |
| `already_auto` | recall was already auto; saves go to `dataset` (same `"pinned"` caveat) |

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
  leaves the session where it is — `"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "<query>" 10 --graph --dataset-id <id>` (see the **cognee-search** skill).

- `COGNEE_PLUGIN_DATASET` only seeds the dataset at launch; a switch overrides it for the rest of the session and survives `--resume`. `COGNEE_PLUGIN_DATASET=auto` is not a dataset name: it means the default dataset (`agent_sessions`) with recall on auto.
- After a switch, recall is restricted to the new dataset, so context from the previous one is no longer injected. `switch-dataset.py auto` puts recall back on auto, which on a cognee >= 1.6.6 server lets recall reach the other readable datasets again. An older server keeps recall on the active dataset.
