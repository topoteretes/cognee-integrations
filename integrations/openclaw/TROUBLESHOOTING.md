# Troubleshooting

Common failure modes for the OpenClaw Cognee plugin, focused on indexing, recall, scopes, and session persistence.

## Cloud cold-start

**Symptom:** `openclaw cognee health`, recall, index, or improve is slow or times out on the first request to a cloud backend.

**Cause:** The Cognee backend may be waking from idle. OpenClaw applies request timeouts, so the plugin can stop waiting before the backend is ready.

**Fix:** Retry after a short wait. For cloud or high-latency deployments, increase `requestTimeoutMs` and `ingestionTimeoutMs` in the `cognee-openclaw` config.

**How to verify:** Run `openclaw cognee health` twice. If the first call times out and the second succeeds without config changes, the failure was likely cold-start latency.

## Embedding-dimension mismatch

**Symptom:** `openclaw cognee index`, recall, or session improvement fails with vector dimension, schema, or shape mismatch errors.

**Cause:** One or more OpenClaw datasets were indexed with a previous embedding model. The active Cognee backend now uses a different embedding model or vector dimension.

**Fix:** Use the original embedding model, or delete and rebuild the affected dataset. For multi-scope mode, check company, user, and agent datasets separately before wiping data.

**How to verify:** Run `openclaw cognee status` to identify the active datasets and scopes, then compare the backend embedding configuration with the one used when those datasets were indexed.

## Wrong conda environment or Python version

**Symptom:** A local Cognee backend used by OpenClaw fails to install, start, or process requests, even though the OpenClaw npm plugin loads.

**Cause:** The local Cognee server is running under Python older than 3.10 or under an unexpected conda/venv environment. The OpenClaw plugin is TypeScript, but the Cognee backend still depends on a supported Python runtime.

**Fix:** Start the Cognee backend from a Python 3.10 or newer environment, then restart the OpenClaw gateway. If you run Cognee through a separate service manager, update that service's Python environment as well.

**How to verify:** In the shell or service that starts Cognee, run `python --version`. Then run `openclaw cognee health` to confirm OpenClaw can reach the corrected backend.

## Session not appearing in the UI after a mode switch

**Symptom:** Session memory or UI history appears missing after changing `mode`, `baseUrl`, API key, user id, or dataset scope during an active OpenClaw session.

**Cause:** OpenClaw session data is associated with the configured backend and dataset scope. Switching local/cloud mode or scope settings mid-session can send later writes to a different backend or dataset than the one the UI is showing.

**Fix:** Finish the current run, update the `cognee-openclaw` config, then restart the OpenClaw gateway before starting a new session.

**How to verify:** Run `openclaw cognee status` and `openclaw cognee scopes` after restart. Confirm the displayed datasets match the UI/backend you are checking.

## Recall returns empty because data was not cognified

**Symptom:** OpenClaw injects no `<cognee_memories>` content even though memory files or previous turns exist.

**Cause:** The relevant files or sessions may not have been indexed into Cognee yet, the wrong scope is being searched, or session QAs have not been bridged into the graph with improve.

**Fix:** Run `openclaw cognee index` for memory files and `openclaw cognee improve` for captured session QAs. In multi-scope mode, confirm the files route to the scope being recalled.

**How to verify:** Run `openclaw cognee status` to check indexed files and dataset mappings, then run `openclaw cognee scopes` to confirm routing. Retry recall after indexing or improve completes.