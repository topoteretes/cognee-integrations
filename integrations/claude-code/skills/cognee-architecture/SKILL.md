---
name: cognee-architecture
description: >-
  Explain a repository's architecture (modules, routes, storage, services), find risky
  hotspots, draw a Mermaid diagram of the code graph, or review what a change did to
  the architecture, from Cognee's code graph.
when_to_use: >-
  E.g. "explain the architecture of this repo", "how is this codebase structured?",
  "where are the hotspots?", "which parts are riskiest to change?", "draw a diagram of
  the code graph", "review my change's impact before I merge". Not for exact lookups
  or indexing (cognee-code); proposing rules (cognee-rules); FigJam diagrams; a bug or
  security review of a diff; or writing CLAUDE.md.
---

# Explain architecture from code facts


## Visible search progress

Before running a Cognee search, send one short user-visible progress sentence,
for example: "Searching Cognee for the earlier decision…" or "Checking Cognee's
code graph for callers of this function…". Name Cognee in the normal working
text before the tool call; keep this to one update per related search batch.
Afterwards, describe relevant findings, an empty result, or a failed search
accurately. Do not claim Cognee improved the answer merely because a search ran.

Run `"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-doctor.sh" --json` and check
`cognee_server` before these queries. `architecture`, `insights`, and diagram output
require server **1.6.3 or newer**. For an older or unknown version, explain the
requirement and stop these operations; do not silently replace them with invented
architecture. Index with the existing `cognee-code` skill if no graph exists.

Cloud indexing requires a git URL, for example:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-index-repo.sh" https://github.com/org/repo --wait 60
```

Cloud facts reflect pushed commits. Identify that revision/freshness when local
changes have not been pushed and re-indexed. Cloud servers cannot read local paths.

Use these existing search commands (the code dataset resolves from the checkout;
add `--dataset <name>` for a different indexed repository):

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "architecture" --code --code-query '{"operation":"architecture","diagram":"mermaid"}'
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "hotspots" --code --code-query '{"operation":"insights","diagram":"mermaid"}'
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "process_payment" --code --code-query '{"operation":"impact_analysis","targets":["process_payment"],"diagram":"mermaid"}'
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "changes" --code --code-query '{"operation":"delta"}'
```

`architecture` gives the module, route, storage, and service map. `insights` gives
Enola explainer findings and their supporting facts. `impact_analysis` follows the
named target's dependencies; replace the example with the user's actual symbol.
`delta` describes changes from the last index, not an arbitrary unindexed diff.

Cite fact names, files, and lines when explaining findings. Separate structural
facts from your interpretation of risk. Render `diagram.source` in a Mermaid block
when requested; if the result has no diagram, say so. Ask for an exact candidate
when a seed is ambiguous. Verify important claims against source before editing.

## Review a change

To review what the working tree (uncommitted edits included) did to the
architecture since it left a base ref, run:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-review.sh" [<base-ref>] [--fail-on <explainers>] [--max-spillover <n>] [--target <symbol-or-package>]
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-review.sh" --stored
```

The base defaults to the remote default branch; when the script says git names
none, ask which ref to compare against. Send the same one-sentence progress
update first ("Asking Cognee to review this change against main…"). The server
runs enola's `check` on the repository and stores the verdict; `--stored` prints
the stored one without running a review.

Nothing is a regression until a policy says so: pass `--fail-on` with the
explainers that should fail (`cycles`, `layers`, `constraints`, `dead-code`, …)
and `--target` with `--max-spillover 0` when the user named what they meant to
change. Without them every finding is reported and the status stays clean.

Report the status, the new findings with their files, the edges added and removed
by kind, and what was reached outside the expected scope. Lead with the line about
changed rule files when it appears: the change then alters the rules it is graded
by, and those edits need the user's eyes first. When the report says the graph was
reloaded since the review, run it again before relying on it.

The review needs a local Cognee server with the code analysis API and a
repository indexed by path; the script says so in one line when the server is
older, is Cognee Cloud, or holds only a clone of pushed commits. Relay that line
and stop; do not substitute a review of your own. A stored regression or changed
rule files are also shown once at the next prompt.
