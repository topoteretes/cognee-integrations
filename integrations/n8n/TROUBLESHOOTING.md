# Troubleshooting

Common failure modes for the n8n Cognee node, focused on workflow order, credentials, backend startup, and empty search results.

## Cloud cold-start

**Symptom:** The first Add Data, Cognify, Search, or Skill operation after idle time is slow or fails with a timeout-like n8n execution error.

**Cause:** Cognee Cloud or a remote backend may need time to wake up. n8n waits for the node operation until its request timeout is reached.

**Fix:** Retry the workflow after a short wait. For long Cognify jobs, use the node version with the longer operation timeout and avoid treating the first timeout as proof that the backend is unavailable.

**How to verify:** Run the same workflow again without changing credentials. If the second run succeeds, the first failure was likely cold-start latency.

## Embedding-dimension mismatch

**Symptom:** Cognify or Search fails with vector dimension, schema, or shape mismatch errors after changing embedding settings on the Cognee backend.

**Cause:** The dataset was previously cognified with one embedding model, then searched or updated with a different embedding model that produces a different vector size.

**Fix:** Use the original embedding model, or delete/recreate the affected dataset and run Add Data followed by Cognify again.

**How to verify:** Check the backend embedding configuration and the dataset used in the n8n node. If the model changed after the dataset was populated, rebuild the dataset before searching.

## Wrong conda environment or Python version

**Symptom:** A self-hosted Cognee backend used by n8n fails to start or returns backend errors, while the n8n node itself still installs correctly.

**Cause:** The n8n node runs on Node.js, but the Cognee backend it calls requires Python 3.10 or newer. A self-hosted backend started from the wrong conda/venv environment can fail before the node receives a useful response.

**Fix:** Start the Cognee backend from a Python 3.10 or newer environment, then rerun the n8n workflow. If using Cognee Cloud, this issue does not apply to the n8n host.

**How to verify:** On the backend host, run `python --version` in the environment that starts Cognee. In n8n, test the Cognee credentials after restarting the backend.

## Session not appearing in the UI after a mode switch

**Symptom:** Workflow data or search results appear under a different Cognee tenant, dataset, or UI than expected after changing credentials or Base URL.

**Cause:** n8n credentials select the target backend. Changing the Base URL, API key, or dataset names between workflow runs can make later operations write to a different tenant or dataset than earlier operations.

**Fix:** Keep one Cognee credential and dataset naming scheme per workflow. After changing credentials, rerun the full workflow from Add Data through Cognify and Search against the same backend.

**How to verify:** Confirm the credential Base URL does not include a trailing `/api`, check the Dataset Name/Datasets fields in each node, and verify all nodes in the workflow use the same Cognee credential.

## Recall returns empty because data was not cognified

**Symptom:** Search returns an empty response even though Add Data completed successfully.

**Cause:** Add Data stores text in the dataset, but Search depends on the data being processed by Cognify. In workflow UIs, it is easy to wire Add Data directly to Search and skip the Cognify node.

**Fix:** Add a Cognify node between Add Data and Search, using the same dataset name. Wait for Cognify to finish successfully before running Search.

**How to verify:** Use the workflow order Add Data -> Cognify -> Search. Confirm the `datasets` value in Cognify and Search matches the `datasetName` value used by Add Data.