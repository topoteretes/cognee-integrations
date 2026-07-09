# Troubleshooting Guide - n8n Integration

This guide documents the 5 most common operational failures encountered when running the n8n Cognee node, detailing their symptoms, root causes, and step-by-step resolutions.

---

## 1. Cloud Cold-Start Latency
### Symptom
* When executing an n8n workflow, the Cognee node fails on the first run with a timeout or network connection error, but succeeds on subsequent manual executions.

### Cause
* The hosted Cognee Cloud backend or serverless remote instance has scaled down to zero due to inactivity. The first API request wakes up the container, causing a temporary delay.

### Fix
* **Allow Warm-up**: Configure retry rules inside the n8n node settings. Under *Node Settings*, toggle **"Retry on Fail"** and set the number of retries (e.g. 3) and delay (e.g. 5000ms).

---

## 2. Embedding-Dimension Mismatch
### Symptom
* n8n executions complete without errors, but return empty recall output silently or throw database exceptions.

### Cause
* You switched embedding model providers (e.g., from OpenAI with 1536 dimensions to a local model with 768 dimensions) after your dataset was already indexed. Existing vectors do not match the size of new query embeddings.

### Fix
* **Clear Vector Database Cache**: Delete your local Cognee databases directory to force a rebuild with the new dimension size:
  * Delete `.cognee_system` (or `.kuzudb` / `.lancedb`) located in your local project root or `.venv/` package path.
* **Re-cognify**: Re-ingest the relevant files to re-index the memory graph.

---

## 3. Wrong Conda/Python Environment (Python < 3.10)
### Symptom
* n8n self-hosted instance fails to spawn the Python process or throws `SyntaxError` / `ModuleNotFoundError: No module named 'cognee'` in the logs.

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
* **Restart the Session**: Clear the current n8n execution context, make sure your configurations are locked, and restart a clean session.

---

## 5. Recall Returns Empty Results (Not Cognified in n8n)
### Symptom
* A Search/Recall node in n8n returns no results even though the preceding Ingestion node successfully processed the text.

### Cause
* Cognee works in two stages: `remember()` stores facts in the fast session cache, but the data is not queryable in the permanent knowledge graph until it runs through the `cognify` extraction pipeline.
* In workflow UIs like n8n, developers frequently link an *Add/Remember* node directly to a *Search* node, forgetting that the graph structure must be compiled first.

### Fix
* **Add a Cognify Node**: Ensure that you include a Cognee Node configured with the action **"Cognify"** (or `improve`) in your n8n workflow pipeline after any ingestion/write operations, before invoking the search/recall nodes.
* **Verify Dataset Scopes**: Check that the `dataset` parameter configured on both the ingestion node and search node matches exactly.
