# Claude Code know-how

## Session management

When a Claude Code session ends, the conversation context is lost. Register
key decisions, rules, and incident records during the session via `remember`,
and the next session can pull them back via `recall`.

## Commit granularity

Run `git commit` immediately after finishing each task. Bundling several tasks
into one commit makes it unclear which change belongs to which task and leaves
the rollback unit ambiguous. One task = one commit is the rule.

## Task management

Before starting work, append the task to the task management table and mark it
in-progress. Editing files without an entry in the table is blocked by a hook.
Register first, work second.

## How to receive feedback

When the user gives feedback, record it immediately, then turn it into a task.
If you start working before recording, you will receive the same feedback
repeatedly. Always follow record -> task -> work.

## git push

Run `git push` only when the user explicitly instructs to. "Please commit"
does not include push. Task completion, phase completion, or project
completion are not reasons to push.

## Full coverage checks

Verification work for testing, review, and audits is exhaustive by default.
Even with many files, do not narrow the scope. Checking only representative
files or only deep-diving into "the important ones" is abandoning quality
assurance.
