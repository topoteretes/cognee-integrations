# Troubleshooting Failures

During integration and hackathons, you may encounter the following common failures. These issues stem from `cognee` core behavior but surface in the integrations.

## 1. Cloud Cold-Start Timeouts
**Symptom:** The first request after a period of idleness or a new deployment is extremely slow or times out completely.
**Root Cause (Core):** `cognee`'s core processing (and remote infrastructure like Modal) has variable cold-start latency. Core does not enforce hardcoded timeouts, meaning the timeout occurs at the client/integration level when the cloud backend takes too long to wake up.
**Fix (Integration):** Increase the timeout configuration in your integration's HTTP client or agent settings to at least 60-120 seconds to allow the backend to cold-start.

## 2. Embedding-Dimension Mismatch
**Symptom:** Errors related to vector shapes or dimension mismatches during memory storage.
**Root Cause (Core):** `cognee` creates vector collections (e.g. via `LanceDBAdapter`) with a specific dimension size. If you switch the embedding model (e.g., in `OllamaEmbeddingEngine`) to one with a different output dimension without recreating the vector store, the insertion fails.
**Fix (Integration):** When changing embedding models in the integration configuration, clear/reset your vector database, or explicitly configure the `embedding_dimensions` to match your new model's output.

## 3. Python Version Errors (Wrong Environment)
**Symptom:** Syntax errors, silent failures, or missing module errors when running the integration locally.
**Root Cause (Core):** `cognee` core enforces `python >= 3.10` in its `pyproject.toml` and relies on modern Python features (like `match...case` and new typing features).
**Fix (Integration):** Ensure your conda, venv, or system Python environment is strictly Python 3.10 or higher.

## 4. Session Not in UI (Mid-Session Mode Flip)
**Symptom:** After flipping modes (e.g., from local to cloud) mid-session, the session disappears from or doesn't register in the frontend UI.
**Root Cause (Core):** Sessions are tracked in `session_records` (`cognee/modules/session_lifecycle/metrics.py`) using a composite key of `(session_id, user_id)`. Flipping modes mid-session often changes the database backend (local SQLite vs Cloud Postgres) or the underlying `user_id` (default vs Auth0). The UI only fetches sessions from the active database/user, causing the session to partition or disappear.
**Fix (Integration):** Do not change the connection mode (local/cloud) mid-session. If you must switch, start a new session ID to avoid state desync.

## 5. Search Returns Empty Instead of Error (Not Cognified)
**Symptom:** When searching or asking questions, the integration returns an empty response `[]` instead of an error, even though you just added data.
**Root Cause (Core):** In `cognee/modules/search/methods/search.py`, if the database/user doesn't exist, a 422 error is raised. However, if data was added but `cognify()` hasn't run yet, the graph is empty (`is_empty = True`). `cognee` only logs a warning and proceeds to return an empty list instead of raising an exception.
**Fix (Integration):** Ensure you trigger the "cognify" process (via the integration's memory sync or dashboard) after adding documents, before you attempt to search or recall information.
