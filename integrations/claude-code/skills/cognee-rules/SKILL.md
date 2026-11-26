---
name: cognee-rules
description: >-
  Propose architecture rules for a repository: candidates mined from Cognee's code
  graph, each with its evidence, exceptions and lint result. Writes an accepted rule
  to enola/constraints/ and remembers rejected ones.
when_to_use: >-
  E.g. "propose architecture rules for this repo", "which conventions does this
  codebase almost follow?", "mine constraints from the code graph", "accept that rule",
  "don't suggest that rule again". Not for reviewing a change or an overview
  (cognee-architecture), exact lookups or indexing (cognee-code), lint or formatter
  settings, or writing CLAUDE.md.
---

# Propose architecture rules


## Visible search progress

Before running a Cognee search, send one short user-visible progress sentence,
for example: "Searching Cognee for the earlier decision…" or "Checking Cognee's
code graph for callers of this function…". Name Cognee in the normal working
text before the tool call; keep this to one update per related search batch.
Afterwards, describe relevant findings, an empty result, or a failed search
accurately. Do not claim Cognee improved the answer merely because a search ran.

## List candidates

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-rules.sh" list [--limit <n>] [--min-confidence <0..1>] [--min-support <n>]
```

The server mines near-invariants from the repository's code graph (regularities
most facts follow, with named exceptions) and lints each candidate shown as a
draft. Nothing is written. Each candidate prints its id, its statement, its
support (`13/15` means 13 of 15 facts follow it), every exception, the lint
result, and the stored `constraint` / `convention` lessons that mention the same
files or symbols. Candidates with such lessons come first: they match something
the user already told Cognee.

Show the user every candidate the script printed, failing ones included. A
candidate that fails lint is not a rule yet; give its reason and do not offer to
accept it. Say how many more candidates were not shown and how many were rejected
earlier, as the first line reports them. Separate what was mined (the counts and
exceptions) from your own view of whether the rule is worth having.

## Accept or reject

Only after the user said yes to a specific candidate:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-rules.sh" accept <id> [--name <file-name>]
```

This writes the rule to `enola/constraints/<name>.yaml` in the working tree. It
never commits and sends nothing to the server. The written `because` text is the
mined statement and the mode is `advisory`: tell the user to rewrite the rationale,
and that changing the mode is what makes a review enforce it. A review that runs
with this file uncommitted reports it under changed rule files; that is expected.

When the user declines a candidate:

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-rules.sh" reject <id> [<id> ...]
```

It is remembered on this machine and not proposed again. Never accept or reject
on your own judgement.

Mining needs a local Cognee server with the code analysis API and a repository
indexed by path (`cognee-index-repo.sh <path>`, see the `cognee-code` skill). When
the script answers with one line saying the server cannot do it, relay that line
and stop; do not invent rules from reading the code.
