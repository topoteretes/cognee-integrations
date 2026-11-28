---
name: cognee-how-it-works
description: >-
  Explain what the Cognee plugin captures, recalls, stores and sends, where its data and
  logs live, and how to inspect or turn off each behavior; check its local or cloud
  connection and settings.
when_to_use: >-
  E.g. "how does Cognee work?", "what does this plugin send to the cloud?", "does it
  capture secrets?", "how do I turn off capture?", "is Cognee connected?", "why was auto
  memory disabled?", "why is memory added to my prompts?". Not for performance or why
  one memory was injected (cognee-report), deleting memory (cognee-forget), or editing
  settings.json, hooks or the status line.
---

# Explain the installed plugin

Read `${CLAUDE_PLUGIN_ROOT}/README.md`, especially “What this plugin runs, stores
and sends”, “Automatic capture controls”, and “Logs and state”. Those shipped
instructions describe this installed version. Run
`"${CLAUDE_PLUGIN_ROOT}/scripts/cognee-doctor.sh" --json` when the user asks about
their connection or settings. Do not expose keys or raw private memory in an answer.

Explain the paths relevant to the question:

- Prompts, answers, and selected tool traces are captured by hooks, with credential
  redaction and sensitive-file exclusions. `COGNEE_CAPTURE=false` opts out.
- Text inside `<private>…</private>` is replaced with `[private]` before it is
  logged, stored, sent as a query or uploaded. This covers explicit remember
  (`/cognee-remember`, `/cognee-handoff`) too, and no setting turns it off. Claude
  still sees the span in the live turn; a `--file` upload is sent unchanged.
- Cloud calls go to `COGNEE_BASE_URL` using `COGNEE_API_KEY`; local mode uses a local
  server. Cloud credit status comes from the Cognee platform API.
- The local server the plugin starts gets `ACCEPT_LOCAL_FILE_PATH=true` unless the user
  set the variable: it lets that loopback-only server read a repository by its path for
  indexing, reviews and rule proposals. A server the user started, and Cognee Cloud,
  are never given it.
- Code reviews and rule proposals are run by the server (enola); the plugin never runs
  enola. Rule proposals run only when asked for. In coding mode a review also runs by
  itself after a Bash call that ran `git commit`, `git push` or `gh pr create` in a
  repository indexed by path, when the change holds code files and that commit was not
  reviewed yet; `COGNEE_AUTO_REVIEW=false` turns that off. A stored review's regression,
  and an automatic review's "Where to look" lines, are shown once at the next prompt.
- In coding mode (the default) nothing is written while the agent works: each request is
  sent once, at its end. Memory is read during the session: up to five lessons at session
  start, recall and a `Lessons for this prompt` list on every prompt, and file context on
  `Read`. `COGNEE_CODING_MEMORY=false` goes back to per-call capture.
- Recall injects selected memory into Claude's context. The injection budget drops
  whole entries and the session ledger avoids repeating cloud memory until context
  is cleared or compacted.
- Local logs and markers live in `~/.cognee-plugin/claude-code/`. Each recall appends a
  body-free receipt (hashes, references, decisions; no prompt or memory text) to
  `sessions/<host id>/recall-receipts.jsonl`, removed with the session folder after a
  week idle. Server memory stays until explicitly deleted. `cognee-report` measures observed outcomes.
- Cloud setup manages native auto-memory only when there is no explicit user choice;
  `COGNEE_DISABLE_AUTO_MEMORY=false` restores the plugin-owned setting.

Distinguish an accepted capture, a landed graph improve, and recalled context.
Consult the installed README for background retries/cache behavior before claiming
it is available. Explain limitations directly when telemetry cannot establish an
outcome.
