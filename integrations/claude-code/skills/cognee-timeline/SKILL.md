---
name: cognee-timeline
description: Chronological view of one topic across sessions — when it was first discussed, what was learned about it and when, and every prompt that named it, in date order. Use when the user asks "when did we…", "how did X evolve", "history of X", or wants the story of a feature, bug or decision over time.
---

# Cognee Timeline

Put one topic on a time axis: the knowledge-graph learnings about it (each
stamped with the session and day it was distilled) merged with the prompts
that mentioned it, oldest first.

## Instructions

1. Run the recap wrapper with the topic as the argument (server-only, no LLM
   call; the graph lookup can take ~10–30 s on a busy local server):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cognee-recap.py" timeline "<topic>" --since 30d \
     || python "${CLAUDE_PLUGIN_ROOT}/scripts/cognee-recap.py" timeline "<topic>" --since 30d
   ```

   The topic is a short phrase as the user would say it ("observer proxy",
   "dataset switching", "Maria Costa") — it seeds the graph search and is
   matched case-insensitively against prompts. `--since` accepts `30d`
   (default), `2w`, `month`, `all`, or a date. `--projects <substr>,…`
   narrows the prompt lane to matching working directories.

2. **Tell the story; do not paste the list.** The output is `## <day>`
   headings with one bullet per event:

   - `[learned · <project> · <agent>]` — a graph passage distilled from that
     session (dated by day; the clock is blank);
   - `[asked · <project> · <agent>]` — a prompt in the window that names the
     topic, with its time.

   Write it as a short chronology: *first appears* → *what changed / was
   decided along the way* (quote learning dates) → *current state / last
   mention*. Flag contradictions between an early learning and a later one —
   that is usually the interesting part.

3. **Empty or thin?** Try a broader `--since`, a synonym the user might have
   typed, or fall back to a plain search
   (`"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh" "<topic>" 10 --graph`)
   to check whether the graph knows the topic under another name. Only
   learnings that were **synced** appear as `learned`; the current session's
   are added by `/cognee-memory:cognee-sync`.

## Notes

- Prompts come from each session's **last 20**; long sessions contribute
  their tail only, and `asked` events carry the session's last-activity time.
- `--json` returns `{topic, events[]}` with `time`, `kind`, `session_id`,
  `project`, `agent`, `text`.
- Server unreachable → one-line stderr reason, exit 1; point at
  `"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-doctor.sh"`.
- Several Claude Code sessions in the same directory? Add
  `--session-key <host session id>`.
