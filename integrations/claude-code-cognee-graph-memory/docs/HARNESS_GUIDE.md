# HARNESS_GUIDE — Claude Code × Cognee Auto-Accumulation Harness

## [Claude Code × Cognee — Practical Know-How Accumulation Tool]

**RAG gives you the answer. Cognee gives you the whole story.**
Claude Code remembers why decisions were made.
**Never say the same thing twice again.**

## What is this harness?

**A mechanism that accumulates your own, personal know-how into Cognee graph memory
the more you use Claude Code.**

- User messages and AI responses are automatically recorded into the graph by hooks
- From the next session onward, the AI is required to search graph memory before
  starting any work
- The same mistakes are not repeated; past decisions, context, and related facts
  come back to you in a connected chain

Plain vector search (RAG) returns only the matching chunk. With graph structure on
top, Cognee returns the rationale, the timeline, and related facts as well — so you
can recover **why** something was decided, **when**, and **what else relates to it**.

---

## How it works

```
┌────────────────────────────────────────────────────────┐
│ Claude Code session                                     │
│                                                         │
│  User message ─────► UserPromptSubmit hook ────┐       │
│                                                ▼        │
│                                  ~/.claude/             │
│                                  cognee_pending_        │
│                                  remembers.jsonl        │
│                                  (queue)                │
│                                                ▲        │
│  AI response done ───► Stop hook ──────────────┘        │
│                                                         │
│  AI runs                                                │
│  search(CHUNKS) ◄─── enforced via CLAUDE.md / rules     │
└────────────────────────┬────────────────────────────────┘
                         │
                         │ flusher drains the queue periodically
                         ▼
                ┌─────────────────────┐
                │ Cognee graph memory │
                │ (persistent)        │
                └─────────────────────┘
```

---

## Installation (5 steps)

### Prerequisites

- The base distribution setup (`docs/SETUP.md`) is complete
- `claude mcp list` shows `cognee` as registered
- `src/sample_src/load_sample.py` has run successfully

### Step 1: Copy harness files into ~/.claude/

```bash
# Run from the distribution root
cp harness/rules/cognee_memory_usage.md ~/.claude/rules/
cp harness/hooks/auto_remember_user_message.py ~/.claude/hooks/
cp harness/hooks/auto_remember_completion.py ~/.claude/hooks/
cp harness/hooks/cognee_remember_flusher.py ~/.claude/hooks/
chmod +x ~/.claude/hooks/auto_remember_user_message.py
chmod +x ~/.claude/hooks/auto_remember_completion.py
chmod +x ~/.claude/hooks/cognee_remember_flusher.py
```

### Step 2: Merge ~/.claude/settings.json

Manually merge the contents of `harness/settings.example.json` into your existing
`~/.claude/settings.json`.

**Important keys to merge**:
- `hooks.UserPromptSubmit` — record user messages
- `hooks.Stop` — record AI response summaries
- `permissions.allow` — auto-approve Cognee MCP tools (`search`, `remember`, etc.)

Append to the existing `hooks` and `permissions` rather than overwriting them.

### Step 3: Append the snippet to your project's CLAUDE.md

Append the contents of `harness/CLAUDE_md_sample.md` to the end of the `CLAUDE.md`
of any project where you want the harness to be active.

This ensures that the AI **always searches Cognee graph memory before starting
any work** in that project.

### Step 4: Schedule the flusher (cron recommended)

The hooks only append to a queue file; the flusher actually performs the
`remember` calls.

**Cron example (every 5 minutes)**:

```bash
crontab -e
# Add the following line:
*/5 * * * * /usr/bin/python3 $HOME/.claude/hooks/cognee_remember_flusher.py >> $HOME/.claude/cognee_flusher.log 2>&1
```

**Or run as a daemon**:

```bash
nohup python3 ~/.claude/hooks/cognee_remember_flusher.py --daemon --interval 60 &
```

### Step 5: Restart Claude Code

Changes to `settings.json` and to the hook files are not picked up by an already
running Claude Code session.

- VSCode Claude Code extension: `Reload Window`
- Terminal `claude` command: exit and re-launch

Verify the connection:
```bash
claude mcp list
# cognee should appear with ✓ Connected
```

---

## Verifying the harness

### Is the harness recording?

1. Send any message to Claude Code
2. `cat ~/.claude/cognee_pending_remembers.jsonl` — your message should appear
3. Run the flusher manually: `python3 ~/.claude/hooks/cognee_remember_flusher.py`
4. Check the log: `cat ~/.claude/cognee_flusher.log` — you should see `OK: dataset=user_messages`
5. From Claude Code, run `search("a keyword from your message", search_type="CHUNKS")`
   — the message should appear in the results

### Is the AI calling search?

Compare the AI's behaviour for the same kind of task before and after enabling the
harness:

- Before: jumps straight into Edit / Bash
- After: first calls `mcp__cognee__search(...)` and only then proceeds

If the after-behaviour does not happen, the snippet may not have made it into your
project's CLAUDE.md.

---

## Troubleshooting

### Hooks aren't running (queue file is not updated)

- Verify the structure of `hooks.UserPromptSubmit` / `hooks.Stop` in `~/.claude/settings.json`
- Did you restart Claude Code? Hook changes require a restart
- Try running `python3 ~/.claude/hooks/auto_remember_user_message.py < /dev/null` standalone

### Flusher fails

- Check `~/.claude/cognee_flusher.log`
- Failed entries are kept in `~/.claude/cognee_failed_remembers.jsonl` for inspection
- If the project root cannot be located, set `COGNEE_GRAPH_MEMORY_ROOT`:
  ```bash
  export COGNEE_GRAPH_MEMORY_ROOT=/path/to/claude-code-cognee-graph-memory
  ```

### The AI doesn't call search

- Confirm the `harness/CLAUDE_md_sample.md` content is in your project's `CLAUDE.md`
- `CLAUDE.md` is read every turn, so a Claude Code restart is not required, but a
  new session may be needed to pick up the change
- For a stronger rule, place `harness/rules/cognee_memory_usage.md` under
  `~/.claude/rules/`

### The queue is filling up faster than it drains

- The flusher is probably not running. Check your cron schedule or the daemon process
- Each entry takes a few seconds to a few tens of seconds to ingest, so the queue
  may be populated on first start
- Drain manually: `python3 ~/.claude/hooks/cognee_remember_flusher.py`

---

## Files in this harness

| File | Purpose |
|---|---|
| `harness/CLAUDE_md_sample.md` | Snippet to append to a project's CLAUDE.md |
| `harness/rules/cognee_memory_usage.md` | Long-form rule for `~/.claude/rules/` |
| `harness/hooks/auto_remember_user_message.py` | UserPromptSubmit hook (queue user messages) |
| `harness/hooks/auto_remember_completion.py` | Stop hook (queue AI response summaries) |
| `harness/hooks/cognee_remember_flusher.py` | Drain the queue (cron / daemon) |
| `harness/settings.example.json` | Example for merging into ~/.claude/settings.json |

---

## Outcome (the longer you use it, the more you accumulate)

- After 1 day: dozens of entries
- After 1 week: hundreds of entries (you can already retrieve past corrections and decisions)
- After 1 month: thousands of entries — your own personal AI knowledge base

**The more you use Claude Code, the more your AI grows into yours.**
