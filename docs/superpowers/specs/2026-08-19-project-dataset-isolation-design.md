# Opt-in Per-Project Dataset Isolation Design

**Date:** 2026-08-19  
**Issue:** [topoteretes/cognee-integrations#356](https://github.com/topoteretes/cognee-integrations/issues/356)  
**Status:** Approved for implementation planning

## Goal

Prevent graph recall from mixing unrelated repositories while preserving the
existing rule that every host conversation has its own Cognee session.

When `COGNEE_DATASET_SCOPE=project` is enabled, Claude Code, Codex, Qwen, and
Antigravity conversations working in the same project use distinct session IDs
inside one stable project dataset. Different projects use different datasets.

The feature is opt-in. Existing installations continue to use
`agent_sessions` unless they select a dataset explicitly or enable project
scope.

## Non-goals

- Do not change how host conversation IDs become Cognee session IDs.
- Do not globally set or pin `COGNEE_SESSION_ID`.
- Do not migrate, merge, copy, delete, or rewrite `agent_sessions`.
- Do not switch datasets during a running session; that remains #292.
- Do not replace the explicit project picker proposed by #285.
- Do not derive identity from branch names, commits, or worktree paths when a
  stable Git remote is available.

## User-visible contract

The shared opt-in setting is:

```bash
COGNEE_DATASET_SCOPE=project
```

The effective dataset precedence is:

1. `COGNEE_PLUGIN_DATASET` from the process environment or `~/.cognee/.env`.
2. An explicit `.cognee/session-config.json` project picker from #285, when that
   feature is present.
3. A derived project dataset when `COGNEE_DATASET_SCOPE=project`.
4. The existing `agent_sessions` default.

`COGNEE_DATASET_SCOPE` accepts `project` case-insensitively after trimming
whitespace. An absent, empty, or unknown value does not change the dataset
selected by another layer and otherwise preserves the existing default. The
global config file remains visibility-only for `dataset`; this feature does not
make `~/.cognee-plugin/config.json` authoritative.

The selected source is carried internally as one of `env`, `picker`, `project`,
or `default`. Internal source metadata is never saved to user config or sent to
Cognee.

## Project identity

Dataset identity is derived locally from the host-reported workspace directory.
The resolver never performs network I/O and never invokes a shell.

### Git repository with a supported `remote.origin.url`

The resolver runs bounded Git subprocesses with argument arrays:

```text
git rev-parse --show-toplevel
git config --get remote.origin.url
```

It normalizes common equivalent remote forms to one identity:

```text
git@github.com:Topoteretes/Cognee.git
ssh://git@github.com/Topoteretes/Cognee.git
https://github.com/Topoteretes/Cognee.git
```

become:

```text
git:github.com/Topoteretes/Cognee
```

Normalization rules are:

- lowercase the host;
- remove URL user information, including embedded credentials;
- remove the default SSH port 22 while preserving a non-default port;
- convert SCP-style SSH remotes to the same host/path form as URL remotes;
- remove leading and trailing path separators and one trailing `.git` suffix;
- preserve repository-path case so case-sensitive Git hosts cannot collide;
- reject an empty host or repository path.

The normalized identity, not the raw remote, is hashed. Raw remotes,
credentials, and full local paths are never logged or placed in dataset names.
An unsupported or malformed remote, including a local-path or `file://` remote,
falls through to the Git-common-directory identity instead of disabling project
isolation.

### Git repository without a usable `origin`

The resolver runs `git rev-parse --git-common-dir`, resolves a relative result
against the workspace, canonicalizes it, and uses:

```text
gitdir:<canonical-common-directory>
```

Linked worktrees therefore share one identity even without a remote. If Git is
unavailable or a bounded Git command fails, the resolver continues to the
canonical workspace fallback.

### Non-Git workspace

The canonical host-reported workspace path becomes:

```text
workspace:<canonical-workspace-path>
```

The host-reported workspace is important: a hook process may execute from the
plugin directory, which is not a valid project identity.

### Dataset name

The emitted name is:

```text
project_<slug>_<hash12>
```

- `slug` comes from the final repository path segment for a normalized remote,
  the directory containing the shared `.git` directory for a Git-common-dir
  identity, or the canonical workspace basename otherwise. It is lowercased,
  converted to ASCII `[a-z0-9-]`, collapsed across repeated separators,
  trimmed, and limited to 32 characters; an empty slug becomes `workspace`.
  Because both identity and slug use shared Git state, linked worktrees emit
  the same complete dataset name, not merely the same hash suffix.
- `hash12` is the first 12 hexadecimal characters of SHA-256 over the normalized
  identity.
- The maximum emitted length is 53 characters.

The readable slug is diagnostic only; the hash is the collision boundary.

## Runtime architecture

### Pure resolver module

Each self-contained Python plugin ships an identical `_project_dataset.py`
module. The module owns:

- remote parsing and normalization;
- bounded Git command execution;
- Git-common-directory and non-Git fallback identity;
- slug and dataset-name generation;
- `derive_project_dataset(workspace: str) -> str | None`.

The integrations remain independently installable, so they cannot import a
runtime file outside their package. Shared tests compare behavior across copies
to prevent drift.

Every Git subprocess uses `shell=False`, captured text output, no stdin, and a
fixed one-second timeout. Resolver failure returns `None`; it does not raise into
a hook.

### Configuration integration

`config.load_config` gains an optional workspace argument and
`COGNEE_DATASET_SCOPE` mapping. Dataset selection occurs after all available
explicit selection layers have run:

```text
defaults -> global config (dataset excluded) -> picker when present -> env
         -> project derivation only when the source is still default
```

The current branch has no #285 picker implementation. The resolver contract
therefore accepts and preserves `_dataset_source="picker"` so a rebase with
#285 has one explicit integration point. If #285 merges first, this branch must
rebase and its picker layer must mark that source. If this feature merges first,
#285 must set the marker when applying a picker value. A compatibility test
pins that contract without copying #285's parser into this change.

An explicit `COGNEE_PLUGIN_DATASET`, including `agent_sessions`, always wins.
Derived dataset values and `_dataset_source` are runtime-only. When first-run
setup creates the global visibility config, it must not persist a project- or
picker-derived dataset as though it were the machine-wide selection.

### One selection per session

Session start resolves the dataset while the host workspace is known and writes
the selected name and source into the existing host-keyed launch-map record.
Subsequent prompt, tool, answer, recall, bridge, and improve paths consume that
pinned value through the existing resolved-session interface.

This prevents a transient Git failure in a later short-lived hook from splitting
one session between the project dataset and `agent_sessions`.

Detached workers continue to receive `COGNEE_SYNC_DATASET`; exit watchers and
session-end sync must propagate the pinned value rather than deriving it again.
Status surfaces prefer resolved-session state and derive only before a session
record exists. Logs may include the source and final dataset name, but never the
raw identity.

### Host workspace inputs

- Claude Code: payload `cwd`, then `CLAUDE_CWD`.
- Codex: payload `cwd`, then `CODEX_CWD`.
- Qwen: host payload workspace/current-directory field, then its host-specific
  cwd environment variable as defined by #354.
- Antigravity: first `workspacePaths` entry or normalized payload `cwd`, then
  `AGY_CWD` as defined by #355.

`os.getcwd()` is only the final fallback for direct scripts. It is never
preferred over a host-provided workspace.

Qwen and Antigravity are open upstream PRs, not part of current `main`. The
first implementation targets Claude Code and Codex. If #354/#355 merge before
the isolation PR is ready, their copies join the same PR; otherwise they receive
follow-up parity changes without blocking the core feature. Local rollout may
apply the reviewed resolver to all four installed integrations after upstream
verification.

## Explicit CLI tools and status surfaces

`cognee-search.sh` and `cognee-remember.sh` currently resolve only
`COGNEE_PLUGIN_DATASET` or `agent_sessions`. Under project scope they must call
the same resolver using the invoking shell's current directory.
`cognee-remember.sh --dataset` continues to override everything;
`cognee-search.sh` keeps its current command-line interface.

Status renderers show the pinned or derived dataset name. A renderer that keeps
the existing standalone/no-network contract reads the host-keyed launch record
using the host session ID in its input, and may import the pure resolver only as
a pre-session fallback. Doctor output reports the effective dataset and source
without exposing the normalized identity.

## Failure handling

- A missing Git binary, nonzero Git exit, timeout, malformed remote, or decoding
  failure skips the affected Git identity layer and falls back to the next
  local identity source. Only failure to produce any valid identity returns
  `None` and preserves `agent_sessions`.
- The resolver writes no files and changes no environment variables.
- One derivation emits at most one bounded fallback/failure hook-log event
  containing an error class, never command output or the raw remote/path.
- An empty explicit dataset is treated as absent, matching current config
  semantics.
- A successfully pinned session dataset is never changed because a later
  resolver attempt fails.

## Testing strategy

### Pure unit tests

- SSH, SCP, HTTPS, credentials, default/non-default ports, `.git`, whitespace,
  malformed remotes, and unsupported local/file remotes.
- Equivalent SSH/HTTPS remotes produce identical names.
- Different normalized repositories produce different names.
- Slug character and length limits.
- Git worktrees and remote-less Git repositories share the expected identity.
- Non-Git canonical-path fallback.
- Missing Git, timeout, nonzero exit, and malformed output fall through to the
  canonical workspace identity; an invalid workspace returns `None`.

### Configuration tests

- scope absent/unknown preserves `agent_sessions`;
- project scope derives only from the default source;
- explicit environment dataset wins;
- simulated picker source wins;
- equivalent Claude Code and Codex workspaces resolve identically;
- session IDs remain distinct across conversations and hosts.

### Pipeline tests

- remember, recall, status, bridge, improve, exit watcher, and final sync use the
  same pinned dataset;
- background workers receive `COGNEE_SYNC_DATASET`;
- the remember wrapper honors `--dataset`; both direct wrappers honor explicit
  env, project scope, and default precedence;
- no test reads or writes the developer's real Git config, home directory,
  credentials, datasets, or memory stores.

The complete shared hermetic suite, Ruff format/lint, JSON/YAML checks, Linux
CI, and Windows CI remain mandatory.

## Documentation and versioning

Claude Code and Codex READMEs document the opt-in setting, exact precedence,
derived-name format, non-migration behavior, and the requirement to remove a
global `COGNEE_PLUGIN_DATASET=agent_sessions` when enabling project scope.

Each changed plugin receives the repository-standard patch-version and
changelog update if required by its packaging convention. Qwen/Antigravity
documentation follows the same wording when their parity changes land.

## Local rollout

Local activation happens only after the upstream branch passes review and
verification:

1. Back up `~/.cognee/.env` and every installed plugin root.
2. Remove or comment the global `COGNEE_PLUGIN_DATASET="agent_sessions"` line.
3. Add `COGNEE_DATASET_SCOPE="project"`.
4. Keep `COGNEE_SESSION_ID` unset.
5. Install the reviewed Claude Code, Codex, Qwen, and Antigravity plugin copies.
6. Restart harnesses one at a time after their active conversations finish.
7. Verify two harnesses in one repository report the same dataset but different
   session IDs, and a harness in another repository reports a different dataset.

The old `agent_sessions` data remains available for manual recall or rollback.
No automatic migration or deletion occurs.
