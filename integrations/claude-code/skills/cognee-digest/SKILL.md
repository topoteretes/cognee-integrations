---
name: cognee-digest
description: Weekly digest of work across all coding-agent sessions — sessions per day and project, most-edited files, and the decisions/learnings Cognee distilled into the knowledge graph in that window. Use when the user asks for a weekly summary, a retro, "what got decided this week", a report for the team, or a recap over several days.
---

# Cognee Digest

A period summary (default: the last 7 days) across every session the Cognee
server recorded, plus the learnings the graph holds for that period. Where
`cognee-standup` answers "what did I do yesterday", the digest answers "what
happened this week and what did we decide".

## Instructions

1. Run the recap wrapper (server-only, no LLM call; the graph lookup can take
   ~10–30 s on a busy local server):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cognee-recap.py" digest --since 7d \
     || python "${CLAUDE_PLUGIN_ROOT}/scripts/cognee-recap.py" digest --since 7d
   ```

   `--since` accepts `7d` (default), `2w`, `month`, `week` (since Monday) or
   a date. `--projects <substr>,…` narrows to matching working directories;
   `--max-sessions N` (default 25) raises the cap for busy weeks;
   `--all-sessions` includes non-coding-agent sessions.

2. **Write the digest from the skeleton; do not paste it.** The output has:

   - a totals line (sessions · prompts · tool calls · distinct files edited ·
     projects);
   - `## <day>` → `### <project>` → one bullet per session (time span,
     counts, first prompt, edited files, last prompts);
   - `## Most-edited files`;
   - `## Learnings recorded in the graph` — passages Cognee distilled from
     sessions **dated inside the window** (each stamped with its date);
   - a closing hint.

   Produce three sections, each a handful of bullets:

   - **Shipped / done** — concrete outcomes per project, inferred from
     prompts + edited files. Merge sessions that continue the same task.
   - **Decided / learned** — from the graph learnings first (they are the
     curated record), then decisions visible in prompts ("let's go with X").
     Quote the learning's date.
   - **Still open** — tasks whose last prompt reads unfinished, sessions with
     errors, anything asked repeatedly.

   Finish with one line on where the time went (which project/day dominated).

3. If stderr says the graph learnings matched **but none are dated in the
   window**, say the graph has not been synced for this period and offer
   `/cognee-memory:cognee-sync` (it distils the current session) — do not
   present older learnings as this week's decisions.

## Notes

- Sessions with *no prompts captured (host without prompt hooks)* came from a
  host that records tool calls but not prompts; describe them by edited files.
- The detail endpoint returns each session's **last 20 prompts and 20 tool
  calls**; the totals line uses the server's full counts.
- `--json` returns `{sessions[], learnings[]}` for a machine-readable digest
  (e.g. to post to Slack).
- Server unreachable → one-line stderr reason, exit 1; point at
  `"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-doctor.sh"`.
- Several Claude Code sessions in the same directory? Add
  `--session-key <host session id>` to resolve the dataset from the right
  launch record.
