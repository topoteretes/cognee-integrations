# Compatibility

What this release was built against and what was actually exercised. A row is a
tested combination, not a promise about neighbours.

## Connector runtime

| Component | Version | Notes |
|---|---|---|
| Node.js | **22.13+** (tested 22.23.2) | `node:sqlite` is used for the receipt ledger; it is unflagged from 22.13 and prints an `ExperimentalWarning` on 22.x, which the `npm` scripts and the Docker image silence with `--disable-warning=ExperimentalWarning`. Node 24 works without that flag. Node 20 is **not** supported (no `node:sqlite`). |
| `@modelcontextprotocol/sdk` | 1.32.2 (pinned) | Streamable HTTP server transport, stateless mode (`sessionIdGenerator: undefined`), `maxRequestBodySize`; tools registered with `registerTool` + zod 3 input schemas. |
| `zod` | ^3.25 | zod 4 is also accepted by the SDK but untested here. |

## Cognee backend

| Backend | Version | Verified |
|---|---|---|
| Self-hosted cognee | **1.6.5** | Live on 2026-10-23: dataset create, background `remember`, cognify status, dataset data listing, `CHUNKS` and `GRAPH_COMPLETION` recall with explicit `datasets` + `scope: ["graph"]`, targeted `forget` by `datasetId`+`dataId`, 401/404 error bodies. Response bodies are kept in [`fixtures/cognee-1.6.5/`](fixtures/cognee-1.6.5/README.md). |
| Cognee Cloud | not verified in this release | Same `/api/v1` routes and `X-Api-Key` auth as self-hosted, but the Cloud deployment was not exercised; run `lovable-memory health` and `npm run test:live` against your tenant before relying on it. |

Observed 1.6.5 behaviours the connector is built around:

- A background `remember` answers `{"status":"running", "dataset_id": …}` with **no document id**
  and `pipeline_run_id: null`. The connector finds its document later by the upload name
  (`<kind>-<sha256[:32]>`) in `GET /datasets/{id}/data`.
- A conflicting upload name (same name, different content) is refused with HTTP 409 only on
  a *synchronous* write; on the background path it is silently dropped. Content-derived
  upload names avoid the conflict; `memory_status` reports a document that never appears as
  `unknown` after a 15-minute grace period instead of pretending it is still indexing.
- `POST /forget` answers `status: "success"` even for a `dataId` that does not exist, so
  the operator `forget` command re-lists the dataset and only marks a receipt deleted when
  the document is gone.
- `POST /recall` with a dataset name that does not exist answers HTTP 404. The connector
  never retries without the `datasets` filter; an empty project dataset (nothing written
  yet) is reported as `empty`.
- A targeted `forget` removes the document from the dataset listing **and** from `CHUNKS`
  recall immediately (re-checked at 0, 5, 15 and 30 seconds after deletion); no delayed
  vector cleanup was observed.
- `GRAPH_COMPLETION` on a dataset that exists but holds no searchable memory (for example
  after its only document was deleted) answers HTTP 404 `NoDataError: No searchable
  memory in dataset …`. The connector reports that as `empty`; `CHUNKS` on the same
  dataset returns `[]`.
- `GRAPH_COMPLETION` returns one synthesized item whose `text` ends with an `Evidence:`
  footer; `only_context: true` returns the full prompt including the caller's recall
  history, which is why answer mode uses the plain completion and labels it as synthesized.

## Lovable

| Surface | Status |
|---|---|
| Custom MCP server, "Bearer token or API key" authentication | Implemented: `Authorization: Bearer <LOVABLE_MCP_TOKEN>` on every request. |
| Live handshake from a Lovable workspace | **Not verified in this release** (no Lovable account in the build environment). The protocol tests use the official MCP TypeScript client over Streamable HTTP; transport choice and any Lovable-side limits still need a real connection, see the README's "Verify in Lovable" checklist. |
| OAuth | Not implemented (v0.1 is single-tenant with a static token). |

## Milestone 0 decision: thin service, not a cognee-mcp preset

The official `cognee-mcp` server (0.5.x, FastMCP, Streamable HTTP) was reviewed as the
alternative. It does not meet the connector requirements through configuration alone: it
has no inbound connector token separate from the cognee API key, exposes `forget`,
`improve`, `cognify_status` and dataset-selecting arguments to the model, and binds no
fixed project scope. The connector therefore keeps the MCP handler (`src/server.ts`,
`src/tools.ts`) separate from the scope/receipt logic (`src/memory.ts`) so a future
official preset with fixed-scope and inbound-auth hooks could replace the handler alone.
