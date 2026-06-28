# Troubleshooting

Common failure modes for the Claude Code plugin, with checks that use the plugin's local logs and session state.

## Cloud cold-start

**Symptom:** The first recall, remember, or sync request after a quiet period is slow, times out, or reports that the request was submitted but confirmation timed out.

**Cause:** A remote or cloud Cognee backend can need extra time to wake up. Claude Code hooks run as client-side subprocesses, so the plugin can hit its HTTP timeout before the backend finishes cold-starting.

**Fix:** Wait briefly and retry the same action. For repeated cold starts, increase the relevant client timeout or keep the backend warm. Do not rerun a timed-out remember request with a different fallback unless logs show a real connection failure, because the write may already have landed.

**How to verify:** Check `~/.cognee-plugin/claude-code/hook.log` and `~/.cognee-plugin/claude-code/subprocess.log`. If the next request succeeds without changing configuration, the first failure was likely a cold start.

## Embedding-dimension mismatch

**Symptom:** Recall or graph sync fails with vector, schema, shape, or dimension mismatch errors after changing embedding settings.

**Cause:** Existing vectors were created with one embedding model, then the plugin or Cognee backend started using a model with a different vector size. The stored collection and the new query/write vectors no longer match.

**Fix:** Use the original embedding model, or clear/recreate the affected vector store and sync the dataset again with the new model. Back up any data you need before deleting local stores or cloud datasets.

**How to verify:** Confirm the active embedding model and dimension in the Cognee backend configuration. If they changed after the `agent_sessions` dataset or your `COGNEE_PLUGIN_DATASET` was already populated, rebuild that dataset with one consistent embedding model.

## Wrong conda environment or Python version

**Symptom:** Local mode fails during startup, imports fail, hooks exit early, or the local Cognee API does not bootstrap.

**Cause:** The shared plugin virtual environment or active shell is using Python older than 3.10. Cognee core requires Python 3.10 or newer.

**Fix:** Start Claude Code from a shell where `python --version` is 3.10 or newer. If the shared plugin venv was created with the wrong interpreter, remove `~/.cognee-plugin/venv/` and restart Claude Code so the plugin can rebuild it with the correct Python.

**How to verify:** Run `python --version` in the same terminal that launches Claude Code, then check `~/.cognee-plugin/claude-code/subprocess.log` for the interpreter used by the plugin.

## Session not appearing in the UI after a mode switch

**Symptom:** A session stops appearing in the UI, or the UI shows a different session, after switching between local and cloud mode during the same Claude Code launch.

**Cause:** Mode, dataset, and session mapping are fixed at launch. Changing `COGNEE_BASE_URL`, `COGNEE_API_KEY`, or dataset settings mid-session can move new writes to a different backend, user, or dataset while the original session map still points elsewhere.

**Fix:** Finish the current session, exit Claude Code, change mode or dataset settings, and start a new session. Use `COGNEE_SESSION_ID` only when you intentionally want to resume a known session in the same backend and dataset.

**How to verify:** Check `~/.cognee-plugin/claude-code/sessions/<host_session_id>.json` and the status line. The session should be looked up under the same mode, dataset, and user that created it.

## Recall returns empty because data was not cognified

**Symptom:** Recall returns no useful results even though prompts, tool traces, or explicit memories were captured.

**Cause:** Capturing data and making it searchable are separate steps. The plugin can enqueue background graph builds, so recall immediately after a write may run before cognify or session sync has completed. Empty results can also come from searching the wrong dataset.

**Fix:** Run `/cognee-memory:cognee-sync`, wait for the background build to finish, or set `COGNEE_REMEMBER_BACKGROUND=false` when you need writes to be queryable immediately. Confirm that `COGNEE_PLUGIN_DATASET` matches the dataset you are recalling from.

**How to verify:** Inspect `~/.cognee-plugin/claude-code/hook.log`, `~/.cognee-plugin/claude-code/watcher.log`, and `~/.cognee-plugin/claude-code/recall-audit.log`. Retry recall after sync completes and confirm the status line shows the expected dataset.