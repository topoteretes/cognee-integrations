# Troubleshooting

Common failure modes for the Hermes Cognee memory provider, especially local-server, remote, and embedded mode setup.

## Cloud cold-start

**Symptom:** Remote mode starts, but the first `cognee_recall`, `cognee_remember`, or session-end improve call is slow or times out.

**Cause:** The remote Cognee service may be waking from idle. Hermes calls Cognee through the provider, so a client timeout can happen before the backend is ready.

**Fix:** Retry after a short wait. If cold starts are common, increase the service startup or request timeout used by the deployment, or keep the Cognee service warm.

**How to verify:** Run `hermes cognee status`, retry the same memory action, and check whether the second request succeeds without changing Hermes configuration.

## Embedding-dimension mismatch

**Symptom:** Recall, remember, or improve fails with vector dimension, schema, or shape mismatch errors.

**Cause:** The Hermes dataset was built with one embedding model, but the current Cognee backend uses another model with a different output dimension.

**Fix:** Restore the original embedding model, or rebuild the affected `COGNEE_DATASET` with the new embedding model. Back up or export important data before clearing local stores.

**How to verify:** Check the Cognee backend embedding configuration and the dataset named by `COGNEE_DATASET`. If the embedding model changed after the dataset was populated, rebuild it before testing recall.

## Wrong conda environment or Python version

**Symptom:** `pip install`, `uv sync`, local-server startup, or embedded mode fails with syntax, typing, or import errors.

**Cause:** Hermes or Cognee is running under Python older than 3.10, or the selected conda/venv is not the one where the provider was installed.

**Fix:** Activate a Python 3.10 or newer environment, reinstall the provider, and rerun `hermes memory setup`. For local-server mode, make sure the server process uses the same supported Python environment.

**How to verify:** Run `python --version`, `hermes cognee config`, and `hermes cognee status` from the same shell that starts Hermes.

## Session not appearing in the UI after a mode switch

**Symptom:** A Hermes session is not visible in the expected Cognee UI after changing between local-server, remote, or embedded mode during the same conversation.

**Cause:** Hermes session memory is written to the backend selected at provider initialization. Changing `COGNEE_BASE_URL`, `COGNEE_EMBEDDED`, `COGNEE_LOCAL_PORT`, or dataset settings mid-session can split session writes across different stores.

**Fix:** Finish the session, stop Hermes, change mode or dataset settings, and start a new session. Avoid silent mode fallbacks; fix the configured backend instead of switching during a running conversation.

**How to verify:** Run `hermes cognee config` before starting the session and confirm the mode and dataset match the Cognee UI you are checking.

## Recall returns empty because data was not cognified

**Symptom:** `cognee_recall` returns no useful memory even though previous turns or explicit memory writes exist.

**Cause:** Session turns may still be in session memory and not yet bridged into graph memory, or `improve()` may not have completed. In embedded mode, background improve work can be lost if the process exits too early.

**Fix:** End the session cleanly so `cognee.improve()` runs, or run the configured improve flow before relying on graph recall. In embedded mode, keep `COGNEE_IMPROVE_BACKGROUND=false` or wait for improve to finish before shutdown.

**How to verify:** Check `hermes cognee status`, confirm `COGNEE_IMPROVE_ON_END=true`, and repeat recall after the session-end improve step completes.