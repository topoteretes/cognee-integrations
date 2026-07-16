# Cognee skill self-improvement loop for n8n

When a skill (e.g. a Claude Code `SKILL.md`) produces a weak run, this workflow
turns that into a **reviewable, approvable** edit to the skill's instructions:
run → score → propose → review the diff → approve → apply. The proposal is always
created first and is never applied until approved.

There are two builds. Pick by who you are:

| | **`beginner/`** — no-code / Verified Node | **`advanced/`** — self-hosted / SDK |
|---|---|---|
| Who | Cloud & no-code users | Terminal-comfortable, self-hosting devs |
| Cognee steps | **Cognee Verified Node** operations (Skill resource) | cognee Python SDK via Execute Command |
| Backend | Any server exposing `/api/v1` (self-hosted now; Cloud as it rolls out) | Local open-source cognee + SDK |
| Cost | Free when pointed at self-hosted cognee + local models | Free (local models) |
| Diff before approve | ✅ via **Get Proposal** | ✅ via `difflib` in the runner |
| Setup | Install the node, set credentials, import `beginner/workflow.json` | Env vars + Execute Command + Python venv |

Both share the same idea and the same example: a `code-review` skill and an
authorization-boundary review task (does the change validate dataset ownership,
handle a missing record, and return the right error?).

- **`beginner/`** — start here if you want a no-code loop. See `beginner/README.md`.
- **`advanced/`** — the original free, complete, self-hosted build. See
  `advanced/README.md`.

> The `beginner/` build requires the Cognee Verified Node (`n8n-nodes-cognee`
> ≥ 0.5.0), which adds the **Skill** resource: Ingest Skill, Review Skill, Propose
> Improvement, Get Proposal, Apply Improvement, Get Skill.
