# Skill routing evals

These cases check that a request in plain words reaches the right Cognee skill,
and that look-alike requests reach none. Each skill directory holds two cases
that should load that skill. `negative/` holds five cases that should load no
`cognee-memory` skill: a git branch switch, a conversational "forget it", a git
sync, a `CLAUDE.md` edit and a general question about how Python works. Every
case allows only the Skill tool and two turns, so nothing runs after the skill
loads. Its grader checks which skill Claude invoked (`tool_used: Skill` with an
`input_match` on the skill name).

## Run

From the repository root:

```bash
claude plugin eval integrations/claude-code --scaffold --ablation none --runs 1 --no-publish
```

- `--scaffold` is required, because it is what isolates the runs. It runs each
  case's `isolate.sh` (see [Isolation](#isolation)). The eval withholds
  `COGNEE_*` variables from your shell, so prefixing the command with settings
  such as `COGNEE_CAPTURE=false` does nothing. Without `--scaffold`, the runs
  are not isolated from Cognee.
- `--ablation none` counts the routing graders in the score. In the default
  run with and without the plugin, `tool_used: Skill` graders show only as
  plugin-fired indicators.
- `--runs 1` is a quick pass. Drop it to get the default three runs per case
  before trusting a description change. To run part of the suite, add
  `--case 'cognee-switch-datasets-*'` or `--case 'negative-*'`.
- The first run asks you to trust the directory. In CI, pass `--trust-plugin`.
- Each run is a real model call on your account. With `--runs 1`, one pass is
  33 runs. Observed cost is about $0.09 to $0.25 per run, $0.16 on average, so
  a full pass costs about $5. Reports go to `evals/results/`, which git ignores.

A positive run normally stops at the two-turn cap after the skill loads. Claude
then looks for a way to run the skill's script, and no shell is granted, so the
harness reports `Reached maximum number of turns (2)`. The grader still scores
the run. A `cognee-handoff` run can finish before the cap. Claude may also hand
the script to the Agent tool, which has no shell either. Add
`--disallow-tools Agent` to skip that detour.

The harness keeps a run's temp directory only when you pass `--keep-temp`.
Delete kept directories with `chmod -R u+rwX <dir>` followed by `rm -rf <dir>`.

## Isolation

The harness gives each run a temporary HOME and passes on only an allowlist of
environment variables plus `EVAL_*`. Exporting
`COGNEE_CAPTURE=false COGNEE_BASE_URL=http://127.0.0.1:9` in your shell therefore
does not reach the plugin's hooks. With nothing configured, the hooks fall back
to `http://localhost:8011`. If a local Cognee server is running there, they
connect to it and capture the eval prompts. If not, they install and boot one in
the background. A loopback URL such as `127.0.0.1:9` gets the same install and
boot.

`isolate.sh` is a scaffold script that writes the run's `$HOME/.cognee/.env`
instead. The harness only runs a scaffold that sits in the case's own directory,
so each case has an identical copy. The script refuses to run if that file
already exists.

| Setting | Effect |
|---|---|
| `COGNEE_CAPTURE=false` | Nothing is captured. |
| `COGNEE_BASE_URL=https://cognee-eval.invalid` | A remote URL that never resolves and could never pass a TLS check. Nothing is booted and recall fails fast. `COGNEE_MANAGED_ENDPOINT` on a loopback URL would also stop the boot, but it adds a "memory offline" warning to Claude's context. |
| `COGNEE_IDLE_DISABLED=true` | No idle watcher or cloud worker keeps running after the run. |

Claude still sees the plugin's normal SessionStart guidance, plus "recall
skipped (server unreachable)" on each prompt. It sees no recalled memory and no
dataset hint, so the suite measures routing from the skill descriptions and
`when_to_use` text alone.
