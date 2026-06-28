# Troubleshooting

Common failure modes for the Codex plugin, with checks that use the plugin's local state, status line, and hook logs.

## Cloud cold-start

**Symptom:** The first recall, remember, or final sync after idle time is slow, times out, or appears to fail before later requests succeed.

**Cause:** A remote Cognee backend can take longer than the Codex hook subprocess timeout to wake up. The backend may still complete the request even if the client stopped waiting.

**Fix:** Wait briefly and retry. If cold starts are frequent, increase the integration timeout where possible or keep the backend warm. Avoid duplicate writes unless logs show a connection failure rather than a confirmation timeout.

**How to verify:** Check `~/.cognee-plugin/codex/hook.log`, `~/.cognee-plugin/codex/subprocess.log`, and `curl -sS http://localhost:8011/health` for local mode. A successful second request with unchanged settings points to cold-start latency.

## Embedding-dimension mismatch

**Symptom:** Memory sync or recall fails with vector dimension, schema, or shape mismatch errors.

**Cause:** Stored vectors were built with one embedding model, then the active Cognee backend was changed to another embedding model with a different output dimension.

**Fix:** Switch back to the original embedding model, or clear/recreate the affected vector store and rebuild the dataset from the captured data. Keep one embedding model per dataset lifecycle.

**How to verify:** Compare the current Cognee embedding configuration with the configuration used when the dataset was first indexed. If they differ, rebuild the dataset before testing recall again.

## Wrong conda environment or Python version

**Symptom:** Local mode fails to start, hook logs show import errors, or the shared local server never becomes healthy.

**Cause:** The plugin is running with Python older than 3.10, or the shared `~/.cognee-plugin/venv/` was created by the wrong interpreter.

**Fix:** Launch Codex from an environment with Python 3.10 or newer. If needed, remove `~/.cognee-plugin/venv/` so the plugin can rebuild it using the correct Python.

**How to verify:** Run `python --version` in the shell that launches Codex, then inspect `~/.cognee-plugin/codex/subprocess.log` for the interpreter and startup errors.

## Session not appearing in the UI after a mode switch

**Symptom:** A session does not appear in the expected UI after changing from local to cloud, cloud to local, or one dataset to another during the same Codex session.

**Cause:** Codex records a session map at launch, and the status renderer reads local env/config state. Switching `COGNEE_BASE_URL`, API keys, or dataset settings mid-session can split writes between different backends or datasets.

**Fix:** Exit Codex, change mode or dataset settings, and start a new session. Reuse `COGNEE_SESSION_ID` only when the backend, user, and dataset are unchanged.

**How to verify:** Check the status line, `~/.cognee-plugin/sessions/<host_session_id>.json`, and `~/.cognee-plugin/codex/hook.log` for the active mode decision.

## Recall returns empty because data was not cognified

**Symptom:** Recall returns empty results even though the plugin captured turns or explicit memories.

**Cause:** Captured session data is not always immediately available in graph recall. Final sync, idle sync, or background graph building may not have completed yet, or recall may be scoped to a different dataset.

**Fix:** Let idle/final sync complete, restart Codex if you changed dataset settings, and confirm `COGNEE_PLUGIN_DATASET` is the dataset you expect. If you need immediate recall, use the explicit sync flow before searching.

**How to verify:** Check `~/.cognee-plugin/codex/watcher.log`, `~/.cognee-plugin/codex/exit-watcher.log`, and `~/.cognee-plugin/codex/recall-audit.log`, then retry the same query after sync finishes.