# cognee 1.6.5 contract fixtures

Response bodies captured on 2026-10-23 from a self-hosted cognee **1.6.5** server
(`uvicorn cognee.api.client:app`, `DEFAULT_USER_PASSWORD` set, API-key auth via
`X-Api-Key`). Server-local file paths were shortened; nothing else was edited.

| File | Request |
|---|---|
| `dataset-create.json` | `POST /api/v1/datasets` `{"name": "lovable_probe"}` (idempotent create-or-return) |
| `remember-background.json` | `POST /api/v1/remember` multipart `data=@memory-abc123.txt`, `datasetName`, `run_in_background=true` — note: no document id and a null `pipeline_run_id`; a background 409 (same upload name, different content) is **not** surfaced in this response |
| `dataset-status-single.json` | `GET /api/v1/datasets/status?dataset=<id>&pipeline=cognify_pipeline` |
| `dataset-data.json` | `GET /api/v1/datasets/<id>/data` — `name` is the upload filename without its extension; `id` is the document (`data_id`) |
| `recall-chunks.json` | `POST /api/v1/recall` `search_type=CHUNKS`, `scope=["graph"]`, explicit `datasets` — `metadata.data_id` / `document_name` carry provenance |
| `recall-graph-completion.json` | same with `search_type=GRAPH_COMPLETION`, `include_references=true` — a synthesized answer with an `Evidence:` footer |
| `recall-dataset-not-found.json` | recall naming a dataset that does not exist → HTTP 404 |
| `unauthorized.json` | wrong `X-Api-Key` → HTTP 401 |
| `forget-document.json` | `POST /api/v1/forget` `{"datasetId", "dataId"}` — also answers `status: "success"` for an id that does not exist, so deletion is verified by re-listing the dataset, never by this body |
