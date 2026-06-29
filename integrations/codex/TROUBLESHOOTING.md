# Troubleshooting Guide - Codex Integration

This guide documents the 5 most common operational failures encountered when running the Codex Cognee memory plugin, detailing their symptoms, root causes, and step-by-step resolutions.

---

## 1. Cloud Cold-Start Latency
### Symptom
* Initial API requests or retrievals hang for 15–30 seconds, or return connection timeout errors on the first query, but subsequent requests work instantly.

### Cause
* The hosted Cognee Cloud backend or serverless remote instance has scaled down to zero due to inactivity. The first API request wakes up the container, causing a temporary delay.

### Fix
* **Allow Warm-up**: Wait 30 seconds and run a simple command to wake up the server.
* **Network Health Check**: Ping your `COGNEE_BASE_URL` directly in the browser or terminal before starting Codex to verify it is online.

---

## 2. Embedding-Dimension Mismatch
### Symptom
* Memory queries return empty results silently or trigger database exceptions. Logs show vector database dimension errors.

### Cause
* You switched embedding model providers (e.g., from OpenAI with 1536 dimensions to a local model with 768 dimensions) after your dataset was already indexed. Existing vectors do not match the size of new query embeddings.

### Fix
* **Clear Vector Database Cache**: Delete your local Cognee databases directory to force a rebuild with the new dimension size:
  * Delete `.cognee_system` (or `.kuzudb` / `.lancedb`) located in your local project root or `.venv/` package path.
* **Re-cognify**: Re-ingest the relevant files to re-index the memory graph.

---

## 3. Wrong Conda/Python Environment (Python < 3.10)
### Symptom
* Codex plugin fails to bootstrap, or throws `SyntaxError` / `ModuleNotFoundError: No module named 'cognee'` in the terminal logs.

### Cause
* The active Conda environment or Python virtual environment is running Python < 3.10. Cognee requires Python versions **between 3.10 and 3.14** due to Kuzu and LanceDB package dependencies.

### Fix
* Confirm the current active Python version:
  ```bash
  python --version
  ```
* If it is below 3.10, create a new virtual environment:
  ```bash
  conda create -n cognee_plugin python=3.10
  conda activate cognee_plugin
  pip install cognee
  ```

---

## 4. Session Missing in UI (Mid-Session Mode Flip)
### Symptom
* You switch the runtime mode (e.g., from local SQLite to remote Cognee Cloud) by exporting new `COGNEE_BASE_URL` or changing config parameters mid-session, but your active session is missing from the UI dashboard.

### Cause
* Cognee reads configuration parameters once during session bootstrap. Flipping configurations or mode states mid-session prevents the client from syncing to the correct database host.

### Fix
* **Restart the Session**: Clear the current Codex instance, make sure your configurations are locked, and restart a clean session.

---

## 5. Recall Returns Empty Results (Not Cognified)
### Symptom
* `recall()` returns no matches or fails to find context, even though your logs confirm that `remember()` successfully captured the data.

### Cause
* Cognee works in two stages: `remember()` stores facts in the fast session cache, but the data is not queryable in the permanent knowledge graph until it runs through the `cognify` extraction pipeline.

### Fix
* **Trigger Consolidation**: Ensure the improvement/cognify pipeline runs to sync cache data to the permanent graph database:
  ```python
  # Ensure your script calls improve
  await cognee.improve()
  ```
* **Verify Dataset Scopes**: Ensure the `COGNEE_PLUGIN_DATASET` matches the target dataset of your recall query.
