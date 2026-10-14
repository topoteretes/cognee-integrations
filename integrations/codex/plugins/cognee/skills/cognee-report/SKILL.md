---
name: cognee-report
description: "Evaluate Cognee from local telemetry. Use for evaluate Cognee, how is Cognee doing or performing, grade, assess or audit Cognee, is Cognee worth it, did the update help, Cognee stats, and memory quality. Reports delivery evidence, quality bars, a measured scorecard and an evidence-based grade. For coding activity summaries use cognee-standup or cognee-digest instead."
---

# Cognee performance report

Use the bundled Python 3.9+ evaluator, ported from team-skills PR 3. It reads local
plugin logs and Codex session rollouts and produces aggregates. It does not send data
anywhere unless an explicit export or server option is requested.

For “how did Cognee perform?”, report the current session and the last seven days.
Default to seven days when no range is supplied; use the user's range when given.

```bash
python3 "${PLUGIN_ROOT}/skills/cognee-report/scripts/plugin_eval.py" report --since 7d --compare
```

Use `--session <cognee-session-id> --since all` for the current session. Resolve the
session from the current hook context or the host's entry in
`~/.cognee-plugin/codex/sessions/<host-session-id>.json`; do not assume a machine-wide last-recall
marker belongs to this terminal. When present, read this host session's
`sessions/<host-session-id>/stats.json` for exact injected/deduped counters.
`--format json` exposes the complete scorecard. `--no-transcripts` is faster but
cannot measure rollout token usage. Hook timings absent from Codex rollouts remain unmeasured. `markers` lists available
date ranges and update times; `--since last-update --compare` evaluates an update.

Present the computed hit rate, recall and SessionStart latency, saves, improves,
injected characters, and grade. Quote evidence from the output, including sample
counts. Mark absent or low-sample metrics as unmeasured. Grades are withheld when
gates fail or evidence is insufficient; never convert that into a positive grade.
Token counts are estimates at four characters per token. Older audit logs are
clipped; say so if exact context counters are unavailable. A recalled or injected
item is not proof that it changed an answer.

For controlled comparisons of memory behavior, use the separate `outcomes` command
and [OUTCOMES.md](OUTCOMES.md). It runs repeated matched task trials and reports
task success, citation correctness, false-memory adoption, tokens/cost and P50/P95
latency. Keep the 45-second cloud recall limit. Distinguish the offline smoke
adapter from live model results; missing usage is unmeasured, not zero.

Keep recommendations tied to measured failures. Compare equal windows when
possible. Do not infer seven-day post-release results from a short smoke test.
Do not use export flags unless the user asks to export the report.

Exact current-session and recent session totals are also available without reading
transcripts:

```bash
python3 "${PLUGIN_ROOT}/scripts/_session_stats.py" --session <host-session-id> --days 7
```

These totals cover sessions that **started** within the window; use the evaluator's
event timestamps for exact calendar-window recall metrics.

For why a specific memory was or was not injected on the last prompt, inspect
its body-free receipt:

```bash
python3 "${PLUGIN_ROOT}/scripts/cognee-evidence.py" explain --session-key <host-session-id>
```

Report per-item reasons (`injected`, `already_in_context`, `over_budget`,
`own_session`, or source availability status), retrieval rank and priority.
Versioned citations can be expanded with `cognee-evidence.py details`. Unaddressable
legacy/guidance entries have hashes only; do not present those as source citations.
The receipt describes delivery selection, not whether an item improved the answer.

The default report leads with **What Cognee did for you**, **Where it helped**,
**Quality**, then the unchanged grade. Omit the latency bar and the entire
**Needs attention** section. Keep latency measurements and gate results in the
scorecard, including low-n markers. Show the generated bars rather than inventing scores. Use `--ascii` for #/- bars or `--format markdown`
for fenced bars. `--full` restores the detailed layout and all recommendation
why/do lines. Session hashes are evidence references, never memory content;
new-token counts from clipped historical bodies are estimates. Delivery quality
is not outcome quality, even when a last-turn receipt exists.
