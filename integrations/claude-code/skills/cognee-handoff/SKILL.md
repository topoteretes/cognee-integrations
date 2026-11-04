---
name: cognee-handoff
description: >-
  Write a concise handoff note for the current work (goal, decisions, open items, next
  steps, branch and PRs) and save it in Cognee so a later session can pick up where the
  work stopped.
when_to_use: >-
  When pausing or wrapping up, e.g. "write a handoff", "I'm done for today, save
  progress", "leave notes for the next session", "checkpoint before I /clear", "save
  where we are and what's next". Not for one fact (cognee-remember), the raw session
  (cognee-sync), a recap of past work (cognee-standup), an in-chat summary, CLAUDE.md
  notes, or a doc or ticket for another person.
---

# Save a handoff

Draft a short note from the current work: goal, outcome, decisions and reasons,
open items, and concrete next steps. Include repository/branch and relevant paths
or PR links. Separate verified results from untested assumptions. Omit credentials
and raw tool output.

Store the note as project knowledge using a temporary UTF-8 Markdown file and the
existing wrapper. Use shell-safe paths; do not interpolate the note into a command.

```bash
"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-remember.sh" --file /tmp/cognee-handoff.md --node-set project_docs
```

Report the write's actual status: queued is not yet queryable. If the wrapper says
`queryable: true`, the next background digest refresh can pick it up. Otherwise
retain the note and report that ingestion is pending or failed. The next session
can explicitly retrieve it with `cognee-search.sh "<project> handoff" --graph`.
A warm digest is cached and may not include a just-written handoff immediately.
