# Cognee Graph Memory Usage Rule (Mandatory)

Place this file under `~/.claude/rules/` so it applies across all projects.

## 1. Search order (mandatory, no exceptions)

Before starting any work, always investigate related knowledge in this order:

```
1. Cognee graph memory: mcp__cognee__search(query, search_type="CHUNKS")
   ↓ if nothing found
2. Read the relevant reference.md under ~/.claude/skills/
   ↓ if nothing found
3. Ask the user
```

## 2. When to apply (search is mandatory)

The following situations require a search before responding:

- Editing, creating, or deleting files (Edit / Write / NotebookEdit)
- Running commands (Bash)
- Design decisions, policy choices
- Suggestions for handling incidents or solving problems
- Presenting options or recommendations to the user

## 3. When not to apply (search not required)

- Simple yes/no answers
- Clarifying the user's intent
- Echoing what the user just said

## 4. Why this order, and why no exceptions

### Why Cognee comes first

- Cognee accumulates **the user's own most recent experience, lessons, and decisions**
- Skills are generic, static, shared across all projects
- Prioritising "specific and recent and user-owned" knowledge prevents repeating
  past mistakes. Skills are the fall-back when Cognee has nothing on this topic.

### Why "no exceptions"

- The cases where you think "I obviously know this" are the cases where you have
  already made the same mistake before
- The act of judging "I can skip the search this time" is itself the source of mistakes
- A search takes seconds. Recovering from a repeated mistake takes hours or days
- "I'll be careful" does not work. Mechanically searching every time does.

### Why "before any work" rather than "every response"

- Searching before every textual reply (chit-chat, intent clarification) would be excessive
- File edits, command execution, and design decisions, however, are **irreversible**
- Therefore: always search before any potentially irreversible action

## 5. How to craft a search query

### Basic

Take 1–3 keywords from the user's request and pass them in directly.

| User request | Example query |
|---|---|
| "What should I watch out for with Django migrate?" | `search("Django migrate caution", search_type="CHUNKS")` |
| "This test is failing, what's wrong?" | `search("test failure cause", search_type="CHUNKS")` |
| "Is it OK to git push?" | `search("git push timing", search_type="CHUNKS")` |

### Variations

- 1st try: only keywords (e.g. `Django migrate`)
- 2nd try: closer to the user's wording (e.g. `What should I watch out for with Django migrate`)
- 3rd try: add related terms (e.g. `Django migrate rollback backup`)

If three tries yield nothing, fall through to skills.

### When handling errors

Pass the error string itself (the most distinctive part) as the query.

```
search("LLMAPIKeyNotSetError Status 422", search_type="CHUNKS")
search("ModuleNotFoundError pydantic", search_type="CHUNKS")
```

## 6. Recording is handled by hooks (you don't have to call remember)

The hooks in `harness/hooks/` automatically register user messages and AI response
summaries into Cognee via `mcp__cognee__remember`.

- The AI does not need to call `remember` explicitly (it is fine to do so; duplicates
  are tolerated)
- When the AI judges that something is particularly important, it may call `remember`
  proactively
- Use these dataset names according to purpose:
  - `feedback` — user-facing corrections / preferences
  - `incidents` — failures, mishandling, error recovery
  - `decisions` — design / policy decisions
  - `lessons` — project-wide takeaways
  - default: `main_dataset`

## 7. Use search(CHUNKS) instead of recall

`mcp__cognee__recall` may fail with an LLM format error when running on
local LLMs other than qwen2.5:14b. The distribution's verification flow
recommends `search(CHUNKS)`.

- For lookups, use `search(query, search_type="CHUNKS")` by default
- Even when `recall` would have been a natural choice, try `search(CHUNKS)` first

## 8. Provenance (how this rule came about)

- 2026-05-02 user instruction: "Cognee first, then fall back to skills"
- Same day: "Always search before any work"
- Same day: "If you skip the search, that's the very moment mistakes happen"
- Same day: when the AI proposed "only record important things to Cognee", the user
  pushed back: "Even if the graph grows huge, you can pull what you need with the
  right query — that's literally the point of graph memory."
  → AI corrected its position: "Record liberally, search before acting."

## 9. Shared agreement

- AI (Claude Code): initially proposed "register only important items to Cognee"
- User: "Even if the graph grows huge, you can extract what you need by searching.
  That's the whole point of graph memory."
- AI: revised stance — "Record liberally is the right operation"
- Both agreed: **record when in doubt; always search before acting**

---

## Related files

- `harness/CLAUDE_md_sample.md` — Snippet to append to your project's CLAUDE.md
- `harness/hooks/auto_remember_user_message.py` — UserPromptSubmit hook
- `harness/hooks/auto_remember_completion.py` — Stop hook
- `harness/settings.example.json` — Example settings.json merge
- `docs/HARNESS_GUIDE.md` — End-to-end installation guide
