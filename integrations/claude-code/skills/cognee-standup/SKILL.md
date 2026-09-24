---
name: cognee-standup
description: "What did I work on since yesterday / this week?" — a per-project standup built from the coding-agent sessions Cognee recorded (prompts, tool calls, edited files, what was left open). Use when the user asks what they did, what happened yesterday/today/this week, or wants a standup, status update or catch-up after time away.
---

# Cognee Standup

Answer "what did I work on?" from the sessions the Cognee server recorded —
every Claude Code / Codex / Antigravity session this identity ran, with their
prompts, tool calls and edited files — grouped by project.

## Instructions

1. Run the recap wrapper (server-only, no LLM call; a few seconds):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cognee-recap.py" standup --since 24h \
     || python "${CLAUDE_PLUGIN_ROOT}/scripts/cognee-recap.py" standup --since 24h
   ```

   Pick `--since` from the request: `24h` (default), `48h`, `today`,
   `yesterday`, `week` (since Monday), `7d`, or a date (`2026-09-20`).
   `--projects <substr>,…` keeps only sessions whose working directory
   matches (e.g. `--projects cognee`). `--all-sessions` adds sessions that are
   not from a coding agent (MCP clients, scheduled jobs).

2. **Summarise the skeleton; do not paste it.** The output is a deterministic
   Markdown skeleton: one `##` per project, one bullet per session with its
   time span, prompt/tool counts, the first prompt (quoted), files edited and
   the last prompts, then a **Left open** section (the last prompt of sessions
   that did not end cleanly). Turn it into a standup:

   - **Yesterday / since last time** — per project, 1–3 lines on what was
     actually done (infer from prompts + edited files; "196 tool calls" is not
     an outcome).
   - **Today / next** — from *Left open* and any prompt that reads like an
     unfinished task.
   - **Blockers** — sessions with `errors` or `failed`, repeated retries of the
     same prompt.

   Keep it short: a standup is read aloud, not studied. Name projects and
   files; skip session ids.

3. Sessions marked *no prompts captured (host without prompt hooks)* were
   driven from a host that records tool calls but not prompts (a Cursor
   terminal, a cron job). Describe them by their edited files and tool mix.

## Notes

- The detail endpoint returns each session's **last 20 prompts and 20 tool
  calls**; long sessions are summarised from their tail. `--json` gives the
  same data structured (`sessions[]` with `prompts`, `files_edited`, `tools`,
  `status`, `duration`) when you need to compute something.
- Windows are by **last activity**: a session started days ago that was
  touched this morning is in today's standup.
- Server unreachable → the wrapper prints a one-line reason on stderr and
  exits 1. Say so and point at
  `"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-doctor.sh"`; do not answer from
  memory of this conversation as if it were the record.
- Several Claude Code sessions in the same directory? Add
  `--session-key <host session id>` so the dataset is resolved from the right
  launch record (the default is the plugin's dataset, `agent_sessions`).
