# Integration env-var reference

This page consolidates the environment variables consumed by the integration packages in `integrations/`.
The aim is to keep the user-facing knobs, defaults, and behavior in one place so the README sections can stay short.

## Primary configuration surface

| Integration | Env var | Default | Effect |
| --- | --- | --- | --- |
| Claude Code | `COGNEE_BASE_URL` | unset | Connect to a managed / cloud Cognee endpoint; when set, local bootstrap is skipped. |
| Claude Code | `COGNEE_API_KEY` | unset | API key for cloud / managed mode; local mode can mint and cache one if absent. |
| Claude Code | `COGNEE_LOCAL_API_URL` | http://localhost:8011 | Override the local API base URL used by the plugin and hooks. |
| Claude Code | `COGNEE_PLUGIN_DATASET` | agent_sessions | Dataset used for writes and recall. |
| Claude Code | `COGNEE_SESSION_ID` | auto-generated per launch | Resume or share a named session instead of generating a fresh one. |
| Claude Code | `COGNEE_SESSION_STRATEGY` | per-directory | How auto-generated session IDs are scoped: `per-directory`, `git-branch`, or `static`. |
| Claude Code | `COGNEE_SESSION_PREFIX` | claude | Prefix for auto-generated session IDs. |
| Claude Code | `LLM_API_KEY` | unset | Required for local mode runtime. |
| Claude Code | `LLM_MODEL` | unset | Model name used in local mode runtime. |
| Claude Code | `COGNEE_PREFER_MEMORY` | true | Steer Claude Code toward Cognee memory instead of native auto memory. |
| Claude Code | `COGNEE_IDLE_POLL` | 10 | Idle watcher poll interval in seconds. |
| Claude Code | `COGNEE_IDLE_THRESHOLD` | 60 | Seconds of inactivity before idle sync fires. |
| Claude Code | `COGNEE_IMPROVE_COOLDOWN` | 120 | Minimum seconds between idle sync runs. |
| Claude Code | `COGNEE_REMEMBER_BACKGROUND` | true | Run remember / cognify in the background so large writes do not block the turn. |
| Claude Code | `COGNEE_REMEMBER_WAIT_SECONDS` | 8 | Bounded wait after a remember call before returning status. |
| Claude Code | `COGNEE_COGNIFY_POLL_INTERVAL` | 3 | Poll interval used while waiting on a background cognify / remember job. |
| Claude Code | `COGNEE_BRIDGE_POLL_DEADLINE` | caller timeout / 600s config default | Overall wait budget for the session→graph bridge. |
| Claude Code | `COGNEE_BRIDGE_SUBMIT_TIMEOUT` | 30 | Read timeout for the background POST that enqueues the job. |
| Claude Code | `COGNEE_STATUS_REQUEST_TIMEOUT` | 10 | Per-poll GET timeout while waiting for status updates. |
| Claude Code | `COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE` | disabled | Clear the transcript after capture for demo-style flows. |
| Claude Code | `COGNEE_USER_EMAIL` | default_user@example.com | Default identity email used when the plugin needs a user identity. |
| Claude Code | `COGNEE_USER_PASSWORD` | default_password | Default identity password used when the plugin needs a user identity. |
| Codex | `COGNEE_BASE_URL` | unset | Connect to a managed / cloud Cognee endpoint; when set, local bootstrap is skipped. |
| Codex | `COGNEE_API_KEY` | unset | API key for cloud / managed mode; local mode can mint and cache one if absent. |
| Codex | `COGNEE_LOCAL_API_URL` | http://localhost:8011 | Override the local API base URL used by the plugin and hooks. |
| Codex | `COGNEE_PLUGIN_DATASET` | agent_sessions | Dataset used for writes and recall. |
| Codex | `COGNEE_SESSION_ID` | auto-generated per launch | Resume or share a named session instead of generating a fresh one. |
| Codex | `COGNEE_SESSION_STRATEGY` | per-directory | How auto-generated session IDs are scoped: `per-directory`, `git-branch`, or `static`. |
| Codex | `COGNEE_SESSION_PREFIX` | codex | Prefix for auto-generated session IDs. |
| Codex | `LLM_API_KEY` | unset | Required for local mode runtime. |
| Codex | `LLM_MODEL` | unset | Model name used in local mode runtime. |
| Codex | `COGNEE_IDLE_POLL` | 10 | Idle watcher poll interval in seconds. |
| Codex | `COGNEE_IDLE_THRESHOLD` | 60 | Seconds of inactivity before idle sync fires. |
| Codex | `COGNEE_IMPROVE_COOLDOWN` | 120 | Minimum seconds between idle sync runs. |
| Codex | `COGNEE_REMEMBER_BACKGROUND` | true | Run remember / cognify in the background so large writes do not block the turn. |
| Codex | `COGNEE_REMEMBER_WAIT_SECONDS` | 8 | Bounded wait after a remember call before returning status. |
| Codex | `COGNEE_COGNIFY_POLL_INTERVAL` | 3 | Poll interval used while waiting on a background cognify / remember job. |
| Codex | `COGNEE_BRIDGE_POLL_DEADLINE` | caller timeout / 600s config default | Overall wait budget for the session→graph bridge. |
| Codex | `COGNEE_BRIDGE_SUBMIT_TIMEOUT` | 30 | Read timeout for the background POST that enqueues the job. |
| Codex | `COGNEE_STATUS_REQUEST_TIMEOUT` | 10 | Per-poll GET timeout while waiting for status updates. |
| Codex | `COGNEE_USER_EMAIL` | default_user@example.com | Default identity email used when the plugin needs a user identity. |
| Codex | `COGNEE_USER_PASSWORD` | default_password | Default identity password used when the plugin needs a user identity. |
| OpenClaw | `COGNEE_MODE` | local | Select `local` for self-hosted Cognee or `cloud` for Cognee Cloud. |
| OpenClaw | `COGNEE_BASE_URL` | http://localhost:8000 | Cognee API base URL. |
| OpenClaw | `COGNEE_API_KEY` | unset | Cognee API key used when authenticating to cloud mode. |
| OpenClaw | `COGNEE_USERNAME` | unset | Username fallback for login when not using an API key. |
| OpenClaw | `COGNEE_PASSWORD` | unset | Password fallback for login when not using an API key. |
| OpenClaw | `OPENCLAW_USER_ID` | unset | User identifier used for user-scoped memory. |
| OpenClaw | `OPENCLAW_AGENT_ID` | default | Agent identifier used for agent-scoped memory. |
| Hermes Agent | `COGNEE_BASE_URL` | unset | Canonical remote / cloud endpoint for Hermes memory mode selection. |
| Hermes Agent | `COGNEE_SERVICE_URL` | deprecated alias of COGNEE_BASE_URL | Legacy alias kept for backward compatibility; lower precedence than `COGNEE_BASE_URL`. |
| Hermes Agent | `COGNEE_EMBEDDED` | false | Run Cognee in-process instead of as a local server. |
| Hermes Agent | `COGNEE_DATASET` | hermes | Default Cognee dataset. |
| Hermes Agent | `COGNEE_TOP_K` | 5 | Number of recall results requested per query. |
| Hermes Agent | `COGNEE_AUTO_ROUTE` | true | Automatically route to the Cognee-backed memory provider. |
| Hermes Agent | `COGNEE_IMPROVE_ON_END` | true | Trigger a graph-improvement pass at session end. |
| Hermes Agent | `COGNEE_IMPROVE_BACKGROUND` | auto | Background the session-end improvement pass in server/remote mode; run inline in embedded mode unless forced. |
| Hermes Agent | `COGNEE_SESSION_PREFIX` | hermes | Prefix for auto-generated session IDs. |
| Hermes Agent | `COGNEE_LOCAL_PORT` | 8000 | Port for the local Cognee server bootstrap. |
| Hermes Agent | `COGNEE_SERVER_BOOT_TIMEOUT` | 30 | How long to wait for the local server to come up. |
| Hermes Agent | `COGNEE_DATA_ROOT` | $HERMES_HOME/cognee/data | Override the Cognee data root used by the plugin. |
| Hermes Agent | `COGNEE_SYSTEM_ROOT` | $HERMES_HOME/cognee/system | Override the Cognee system root used by the plugin. |
| Hermes Agent | `COGNEE_RECALL_TIMEOUT` | 60 | Per-query recall timeout used by Hermes memory lookups. |
| Hermes Agent | `COGNEE_WRITE_TIMEOUT` | 120 | Timeout for write/remember operations. |
| Hermes Agent | `COGNEE_IMPROVE_TIMEOUT` | 300 | Timeout for the improve pass. |
| Hermes Agent | `COGNEE_HERMES_USER_EMAIL` | hermes-agent@cognee.local | Default Hermes identity email. |
| Hermes Agent | `COGNEE_HERMES_USER_PASSWORD` | hermes-agent-plugin | Default Hermes identity password. |
| n8n | `COGNEE_SELF_IMPROVE_WORKFLOW_ROOT` | relative to repo root | Locate the `advanced/` workflow wrapper from inside n8n. |
| n8n | `COGNEE_REPO` | unset | Path to the Cognee repo used by the advanced workflow runner. |
| n8n | `COGNEE_PYTHON` | $COGNEE_REPO/.venv/bin/python → python3 → python | Python executable used by the advanced workflow runner. |
| n8n | `NODES_EXCLUDE` | [] | Security override for n8n 2.x so the local demo can use Execute Command. |
| n8n | `N8N_PORT` | 5680 | Port used when starting local n8n for the advanced demo. |
| n8n | `COGNEE_SELF_IMPROVE_SMOKE` | 0 | Initialize Cognee and ingest skills only. |
| n8n | `COGNEE_SELF_IMPROVE_DRY_RUN` | 0 | Run the workflow without applying the proposal. |
| n8n | `COGNEE_SELF_IMPROVE_PRUNE` | 0 | Clear Cognee data/system metadata before the run. |
| n8n | `COGNEE_SELF_IMPROVE_APPLY` | 1 | Apply the proposal after review when approval allows it. |
| n8n | `COGNEE_SELF_IMPROVE_SYNC_FILE` | 1 | Rewrite `SKILL.md` after applying the proposal. |
| n8n | `COGNEE_SELF_IMPROVE_SYSTEM_ROOT` | workflow-local `.cognee_system` | Override the system store used by the workflow. |
| n8n | `COGNEE_SELF_IMPROVE_DATA_ROOT` | workflow-local `.cognee_data` | Override the data store used by the workflow. |
| n8n | `SYSTEM_ROOT_DIRECTORY` | unset unless supplied by the runner | Absolute system-store path used by the SDK / workflow. |
| n8n | `DATA_ROOT_DIRECTORY` | unset unless supplied by the runner | Absolute data-store path used by the SDK / workflow. |
| n8n | `COGNEE_SKILL_SOURCE_ROOTS` | `integrations/.../my_skills` plus any existing entries | Extra skill roots searched before the runner starts. |
| n8n | `COGNEE_SKILL_SCORE` | 0.3 | Evaluator score recorded in the run entry. |
| n8n | `COGNEE_SKILL_SCORE_FROM_AGENT` | 0 | Use the agent's JSON score instead of the fixed `COGNEE_SKILL_SCORE`. |
| n8n | `COGNEE_SKILL_SCORE_THRESHOLD` | 0.9 | Score threshold below which an improvement proposal is created. |
| n8n | `COGNEE_SELF_IMPROVE_APPROVED` | 1 | Whether the proposal is approved for application. |
| n8n | `COGNEE_SKILL_NAME` | code-review | Skill name used in the self-improve runner. |
| n8n | `COGNEE_SKILL_DATASET` | n8n-skill-self-improvement | Dataset used by the self-improve runner. |
| n8n | `COGNEE_SKILL_SESSION` | n8n-skill-self-improvement-session | Session name used by the self-improve runner. |
| n8n | `COGNEE_SELF_IMPROVE_STATE` | workflow-local state file | JSON state file used to persist the current run. |
| n8n | `COGNEE_SKILL_TASK` | built-in demo task text | Task text fed into the skill loop when none is provided. |
| n8n | `COGNEE_SELF_IMPROVE_RUN_ID` | time-based ID | Unique run identifier recorded in the state file. |
| n8n | `COGNEE_SKILL_MAX_ITER` | 6 | Max iterations passed to the agentic review step. |

## Runtime / helper vars

These are read directly by launcher or helper scripts. They are still part of the consumed env surface, but they are more plumbing-oriented than the main config knobs above.

| Integration | Env var | Default | Effect |
| --- | --- | --- | --- |
| Claude Code / Codex | `COGNEE_BREAKER_THRESHOLD` | 5 | Circuit-breaker failure threshold before recall falls back. |
| Claude Code / Codex | `COGNEE_BREAKER_COOLDOWN` | 120 | How long the circuit breaker stays open after tripping. |
| Claude Code / Codex | `COGNEE_RECALL_TIMEOUT` | 20 | Per-call timeout for explicit recall requests. |
| Claude Code / Codex | `COGNEE_RECALL_BUDGET` | 4 | Overall time budget for one prompt's recall pass. |
| Claude Code / Codex | `COGNEE_READY_PROBE_TIMEOUT` | 1 | One-shot /health probe timeout before skipping recall on a warming backend. |
| Claude Code / Codex | `COGNEE_SERVER_BOOT_DEADLINE` | 600 | Deadline for local-server bootstrap. |
| Claude Code / Codex | `COGNEE_LAZY_BOOTSTRAP` | 1 / true | Enable lazy bootstrap of the local runtime unless explicitly disabled. |
| Claude Code / Codex | `COGNEE_INSTALL_TIMEOUT` | 600 | Timeout used while installing or refreshing the local runtime. |
| Claude Code / Codex | `COGNEE_PLUGIN_PYTHON` | 3.12 | Python interpreter pin used by the launcher. |
| Claude Code / Codex | `COGNEE_PLUGIN_STATE_DIR` | ~/.cognee-plugin | Root directory for hook state, logs, and cached metadata. |
| Claude Code / Codex | `COGNEE_IDLE_DISABLED` | false | Opt out of the idle watcher / idle sync path. |
| Claude Code / Codex | `COGNEE_AGENT_NAME` | codex-agent / claude-code-agent | Agent name stored in the launch config and session metadata. |
| Claude Code / Codex | `COGNEE_USER_ID` | runtime-resolved | User identity resolved during startup and reused by helper scripts. |
| Claude Code / Codex | `COGNEE_SESSION_KEY` | runtime-resolved | Host-session correlation key used by helper scripts. |
| Claude Code / Codex | `COGNEE_SYNC_SESSION_ID` | runtime-resolved | Detached sync worker session identifier. |
| Claude Code / Codex | `COGNEE_SYNC_DATASET` | runtime-resolved | Detached sync worker dataset identifier. |
| Claude Code / Codex | `COGNEE_SYNC_START_DELAY` | runtime-resolved | Start delay for detached sync workers. |
| Claude Code / Codex | `COGNEE_SYNC_RETRIES` | runtime-resolved | Retry count for detached sync workers. |
| Claude Code / Codex | `COGNEE_SYNC_RETRY_DELAY` | runtime-resolved | Retry delay for detached sync workers. |
| Claude Code / Codex | `COGNEE_UNREGISTER_ON_FINISH` | 0 / unset | Signals the detached worker to unregister when it exits. |

## Notes

- `COGNEE_SERVICE_URL` is retained only as a deprecated alias for `COGNEE_BASE_URL` in Hermes Agent.
- The Claude Code and Codex integrations share most of the same runtime plumbing; the table keeps both defaults where they differ.
- The n8n rows above cover the self-improving skill workflow in `n8n_workflows/cognee_skill_self_improve/`.
