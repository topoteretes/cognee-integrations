# Cognee Skill Self-Improvement n8n Workflow

This folder contains a local n8n workflow that runs Cognee's skill
self-improvement loop. The workflow is split into visible n8n stages:

1. configure score, threshold, approval, and file sync controls
2. initialize run state
3. remember local skills in Cognee
4. run the Cognee agent with the selected skill
5. branch on `score < score_threshold`
6. record `SkillRunEntry` feedback and request a proposal
7. build a review packet
8. branch on approval
9. apply the proposal and emit the final diff
10. show the skill delta in a dedicated `Show Skill Delta` node

For n8n 2.x, start local n8n with Execute Command enabled:

```bash
export COGNEE_SELF_IMPROVE_WORKFLOW_ROOT="$PWD/n8n_workflows/cognee_skill_self_improve/advanced"
export COGNEE_REPO="/path/to/cognee"
export COGNEE_PYTHON="$COGNEE_REPO/.venv/bin/python"
NODES_EXCLUDE=[] N8N_PORT=5680 npx n8n
```

n8n 2.x excludes `n8n-nodes-base.executeCommand` and
`n8n-nodes-base.localFileTrigger` by default for security. This local demo
needs `executeCommand` because the final `improve_skill(..., apply=True)` step
is currently available through the Cognee Python SDK, not the published Cognee
n8n HTTP node.

The workflow uses:

- `my_skills/code-review/SKILL.md` as the starter skill.
- `run_self_improve_skill.py` as the runner with subcommands.
- `run_n8n_action.sh` as the portable n8n command wrapper.

The workflow JSON does not contain machine-specific paths. The wrapper resolves
Python in this order:

1. `COGNEE_PYTHON`
2. `$COGNEE_REPO/.venv/bin/python`
3. `python3`
4. `python`

If you start n8n from the repository root, the workflow can find the wrapper via
the relative fallback `./n8n_workflows/cognee_skill_self_improve/advanced`. If you
start n8n from another directory, set `COGNEE_SELF_IMPROVE_WORKFLOW_ROOT` to the
absolute path of this folder.

The runner still supports the old single-command path:

```bash
python run_self_improve_skill.py run-full
```

The n8n workflow uses the more inspectable subcommands:

```bash
python run_self_improve_skill.py init-state
python run_self_improve_skill.py remember-skills
python run_self_improve_skill.py run-agent
python run_self_improve_skill.py record-feedback
python run_self_improve_skill.py review-packet
python run_self_improve_skill.py apply-proposal
```

Each subcommand prints JSON for the next n8n node to parse.

After a successful apply, click the `Show Skill Delta` node in n8n. It exposes:

- `skill_delta`: raw unified diff between the previous and improved skill body.
- `skill_delta_markdown`: a Markdown-ready diff block for Slack, GitHub, Linear,
  Notion, or email.

By default the runner uses workflow-local Cognee storage:

- `.cognee_system`
- `.cognee_data`

That keeps this n8n demo from trying to open the same Ladybug/Kuzu graph files
as a running Cognee UI/backend process. To intentionally use another Cognee
store, set `SYSTEM_ROOT_DIRECTORY` and `DATA_ROOT_DIRECTORY`, or the demo-local
aliases below.

Useful environment knobs:

- `COGNEE_SELF_IMPROVE_SMOKE=1`: initialize Cognee and ingest skills only.
- `COGNEE_SELF_IMPROVE_PRUNE=1`: clear Cognee data/system metadata first.
- `COGNEE_SELF_IMPROVE_APPLY=0`: propose but do not apply.
- `COGNEE_SELF_IMPROVE_SYNC_FILE=0`: apply in graph only; do not rewrite `SKILL.md`.
- `COGNEE_SELF_IMPROVE_SYSTEM_ROOT=/path/to/system`: override workflow-local system storage.
- `COGNEE_SELF_IMPROVE_DATA_ROOT=/path/to/data`: override workflow-local data storage.
- `COGNEE_SKILL_SCORE=0.3`: evaluator score recorded in `SkillRunEntry`.
- `COGNEE_SKILL_SCORE_FROM_AGENT=1`: use the agent's JSON score instead of `COGNEE_SKILL_SCORE`.
- `COGNEE_SKILL_SCORE_THRESHOLD=0.9`: threshold below which proposals are created.
- `COGNEE_SELF_IMPROVE_APPROVED=0`: generate a review packet but do not apply.

The imported workflow has a `Demo Controls` node where these values can be
edited without changing Python.
