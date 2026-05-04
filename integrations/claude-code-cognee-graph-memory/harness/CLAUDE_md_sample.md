# CLAUDE.md Snippet — Rules to make Claude Code use Cognee graph memory automatically

Append the contents of this file to the end of your project's `CLAUDE.md`.
Claude Code reads `CLAUDE.md` every turn, so the rules below are always
in effect.

---

The following block is the part to copy and paste into your `CLAUDE.md`:

```markdown
## 🔴 Knowledge search order (mandatory rule)

Before starting any work, always investigate related knowledge in this order. No exceptions.

### Search order

1. **Search Cognee graph memory first**
   Call `mcp__cognee__search(query, search_type="CHUNKS")` to retrieve user-specific
   experiences, lessons, and decisions accumulated across past sessions.
2. **If Cognee yields nothing, read `~/.claude/skills/`**
   Read the relevant `reference.md` of the matching skill.
3. **Only if both fail, ask the user**

### When to apply (search is mandatory)

- Any response that edits, creates, or deletes files
- Any response that runs commands (Bash etc.)
- Design decisions, policy choices, presenting options
- Suggestions for handling incidents or solving problems

### When not to apply (search is not required)

- Simple yes/no answers
- Clarifying the user's intent
- Echoing what the user just said

### Why this order?

- Cognee holds the most recent, user-specific experience. Skills are static, generic, shared knowledge.
  Prioritising the more concrete and more recent source prevents repeating past mistakes.
- The cases where you "obviously already know it" are the very cases where you have
  made the same mistake before. Search costs seconds. A repeated mistake costs hours or days.
- The act of judging "I can skip this search" is itself the source of mistakes.
  Eliminate that judgment by always searching.

### Crafting search queries

- Pick 1–3 keywords from the user's request and pass them in
  - Example: "What should I watch out for with Django migrate?"
    → `search("Django migrate caution", search_type="CHUNKS")`
- File names, function names, and error strings can be passed in as-is
- If a query yields nothing, retry 2–3 times with different wordings before
  falling through to skills

### Recording is handled by hooks (you do not have to call remember)

The hooks in `harness/hooks/` automatically record user messages and AI response
summaries via `mcp__cognee__remember`. The AI does not need to call `remember`
explicitly (calling it manually is also fine; duplicates are tolerated).
```

---

## What this snippet is

- This file is part of the distribution and is intended to be **copy-pasted as-is into your `CLAUDE.md`**
- You may trim parts to suit your own project, but **do not remove "Search order" or "When to apply"** — they are the core of this harness
- Together with `harness/rules/cognee_memory_usage.md` placed under `~/.claude/rules/`, you also get the long-form rule (with rationale and provenance) that the AI can refer to

## Related files

- `harness/rules/cognee_memory_usage.md` — Long-form rule (Why / provenance / shared agreement)
- `harness/hooks/auto_remember_user_message.py` — User message → Cognee
- `harness/hooks/auto_remember_completion.py` — AI response → Cognee
- `harness/settings.example.json` — Example settings to enable the hooks
- `docs/HARNESS_GUIDE.md` — End-to-end installation guide
