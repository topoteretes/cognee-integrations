<div align="center">
  <a href="https://www.cognee.ai">
    <img src="https://raw.githubusercontent.com/topoteretes/cognee-integrations/main/assets/cognee-logo.svg" alt="Cognee" width="260">
  </a>
  <p><strong>Cognee product memory for Lovable</strong> — a scoped remote MCP connector that lets the Lovable builder recall product decisions and evidence, save accepted decisions with their rationale, and check when they are searchable.</p>
  <p>
    <a href="https://docs.cognee.ai">Docs</a> ·
    <a href="https://discord.gg/NQPKmU5CCg">Discord</a> ·
    <a href="https://github.com/topoteretes/cognee">Cognee core</a> ·
    <a href="https://docs.lovable.dev/integrations/custom-mcp">Lovable custom MCP</a>
  </p>
</div>

# Cognee Product Memory Connector for Lovable

Lovable already remembers *instructions* through workspace and project knowledge. This
connector adds the other half: **changing, source-backed product knowledge** — the
decisions you accepted, why, and where the evidence came from — retrieved when it is
relevant and saved when the user agrees.

It is a small TypeScript service you host (one container) that Lovable reaches as a
[custom MCP server](https://docs.lovable.dev/integrations/custom-mcp) over HTTPS with a
bearer token. It talks to a Cognee backend (Cognee Cloud or a self-hosted server, cognee
**1.6.5** verified) with a server-held API key that never reaches Lovable or the model.

## What the builder gets

| Tool | Model-controlled inputs | What it does |
|---|---|---|
| `recall_product_context` | `query`, optional `limit`, optional `mode` (`evidence` default, `answer`) | Searches only this deployment's datasets and returns stored passages with their record names and scores, or a synthesized answer labelled as such. Says `empty`, `unavailable` or `error` honestly. |
| `remember_product_decision` | `content`, optional `rationale`, `source_reference` (a provenance string, never fetched), `request_id` | Writes one bounded, provenance-stamped record into the fixed project dataset and returns a **receipt**. Replays with the same `request_id`, or identical content, return the existing receipt instead of a duplicate. |
| `memory_status` | `receipt_id` | `accepted` → `indexing` → `ready`, or `failed` / `unknown` / `deleted`, checked against the backend. |

Not exposed to the model, by design: backend URLs, API keys, dataset selectors, URLs to
ingest, files, deletion. Corrections and deletion are an authenticated operator CLI.

Everything a Lovable chat sees is **scoped**: one project dataset is written and read,
plus an explicit list of read-only shared datasets (for example a customer-research
dataset shared across two projects). Results naming any other dataset are dropped before
they are returned. A second isolated project gets a second deployment with its own token.

## Quick start

### 1. Get a Cognee backend and API key

- **Cognee Cloud:** copy the tenant API base URL and an API key from *Cognee Cloud → API
  Keys*.
- **Self-hosted:** run cognee 1.6.5 (for example `cognee/cognee:1.6.5` with
  `DEFAULT_USER_PASSWORD` set and an `LLM_API_KEY`), log in once and create an API key
  (`POST /api/v1/auth/api-keys`); see
  [`integrations/CONFIGURATION.md`](../CONFIGURATION.md#default-user-password-cognee--160).

### 2. Run the connector

```bash
cd integrations/lovable
cp deploy/.env.example deploy/.env
npx @cognee/lovable-memory token      # paste the output into LOVABLE_MCP_TOKEN
# fill COGNEE_BASE_URL, COGNEE_API_KEY, LOVABLE_PROJECT_DATASET, LOVABLE_PUBLIC_URL
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml exec lovable-memory node dist/src/operator-cli.js health
```

Without Docker (Node **22.13+**): `npm install -g @cognee/lovable-memory`, export the same
variables, then `lovable-memory health` and `lovable-memory serve`.

The connector listens on `127.0.0.1:8787` over plain HTTP. **Lovable needs a public
HTTPS URL**, so put a TLS reverse proxy in front. `deploy/Caddyfile.example` is a complete
Caddy configuration with automatic certificates; any proxy that passes `/mcp` through
without buffering server-sent events works. Running the connector only on a laptop's
localhost does not make it reachable by Lovable.

Set `LOVABLE_PUBLIC_URL` to that public `https://` address: its hostname joins the Host
allowlist and its origin the Origin allowlist (DNS-rebinding protection is never
disabled to make a hostname work — declare it instead).

### 3. Connect it in Lovable

In your Lovable project: **Connectors → Add custom → MCP server**, enter a name and the
public URL with the `/mcp` path (for example `https://memory.example.com/mcp`), choose
**Bearer token / API key** authentication and paste the `LOVABLE_MCP_TOKEN` value, then
**Add server**. Custom MCP connections are personal: every workspace member connects with
their own copy of the token.

Test with a prompt the server can answer, for example
*"Use recall_product_context to check what we decided about onboarding login."*

### 4. Teach the project to use it

Paste [`examples/project-knowledge.txt`](examples/project-knowledge.txt) into the
project's **Knowledge**. It tells Lovable to recall before substantial changes and to save
accepted decisions (with a stable `request_id`) afterwards. These instructions encourage
tool calls; they cannot guarantee that every turn is captured. The connector logs which
tools actually ran (`LOVABLE_LOG_LEVEL=info`).

### 5. Verify in Lovable (first-run checklist)

The protocol is exercised in this repository with the official MCP TypeScript client, but
a Lovable workspace was not available to the build, so confirm once on your side:

1. The connector lists exactly three tools in Lovable's connector view.
2. A chat saves a synthetic decision (`remember_product_decision`) and receives a receipt.
3. `memory_status` reaches `ready` (graph building takes a few seconds to minutes).
4. A **new** chat recalls it with `recall_product_context`.
5. `lovable-memory receipts` shows the record; `lovable-memory forget <id> --yes` removes
   it and a further recall no longer returns it.

## Configuration

Precedence per field: explicit `resolveConfig()` argument (library use) → environment →
default. Missing required values stop startup with every problem listed.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `COGNEE_BASE_URL` | yes | `http://localhost:8000` | Cognee backend (Cloud tenant URL or self-hosted server) |
| `COGNEE_API_KEY` | yes | — | Backend API key, sent as `X-Api-Key`; never leaves the server |
| `LOVABLE_PROJECT_DATASET` | yes | — | The one dataset written and always read; created on first write |
| `LOVABLE_SHARED_DATASETS` | no | — | Comma-separated read-only datasets; must already exist, never created |
| `LOVABLE_MCP_TOKEN` | yes | — | Inbound bearer token(s), comma-separated to overlap a rotation; ≥ 16 chars |
| `LOVABLE_PUBLIC_URL` | recommended | — | Public `https://` URL; feeds the Host and Origin allowlists |
| `LOVABLE_ALLOWED_HOSTS` / `LOVABLE_ALLOWED_ORIGINS` | no | loopback + public URL | Explicit allowlists (replace the derived ones); `*` disables the check |
| `LOVABLE_MCP_HOST` / `LOVABLE_MCP_PORT` / `LOVABLE_MCP_PATH` | no | `127.0.0.1` / `8787` / `/mcp` | Bind address and endpoint path |
| `LOVABLE_RECEIPTS_PATH` | no | `./data/receipts.sqlite` | Receipt ledger (SQLite). Put it on a persistent volume; `:memory:` is for tests and is reported as non-durable |
| `LOVABLE_RECALL_TOP_K` / `LOVABLE_MAX_RECALL_RESULTS` / `LOVABLE_MAX_RECALL_CHARS` | no | 5 / 10 / 6000 | Recall budget |
| `LOVABLE_MAX_CONTENT_CHARS` / `LOVABLE_MAX_RATIONALE_CHARS` / `LOVABLE_MAX_QUERY_CHARS` | no | 4000 / 2000 / 500 | Input caps (longer input is truncated) |
| `LOVABLE_RECALL_MODE` / `LOVABLE_RECALL_ALLOW_ANSWER_MODE` | no | `evidence` / `true` | Default recall mode; set the flag to `false` to refuse synthesized answers |
| `LOVABLE_MAX_REQUEST_BODY_BYTES` | no | 262144 | Inbound body cap (HTTP 413 above it) |
| `LOVABLE_REQUEST_TIMEOUT_MS` / `LOVABLE_WRITE_TIMEOUT_MS` / `LOVABLE_RECALL_TIMEOUT_MS` / `LOVABLE_STATUS_TIMEOUT_MS` | no | 15000 / 30000 / 20000 / 10000 | Backend deadlines |
| `LOVABLE_LOG_LEVEL` | no | `info` | `debug` / `info` / `warn` / `error`; logs are JSON on stderr with secrets redacted |

`lovable-memory config` prints the resolved configuration with secrets masked.

## Memory records and reliability

Each saved decision becomes one text document in the project dataset:

```
Product decision (Lovable project memory, dataset lovable_my_product)
Recorded: 2026-10-23T18:54:46Z
Source reference: feedback-2026-10-01

Decision:
Use passwordless magic-link login for the onboarding flow.

Rationale:
Password resets were the top support ticket in 12 customer interviews.
```

Provenance lives in the text so it survives any backend version's metadata handling. The
upload filename is derived from the content (`decision-<sha256>`), which is the document's
identity on the server: identical content is a no-op, different content gets a new name,
and the conflict a reused name would cause is never triggered.

**Receipts.** Every accepted write is a row in the SQLite ledger before the backend is
called, with the `request_id` (or a content-derived one) as a unique key. Statuses:

| Status | Meaning |
|---|---|
| `accepted` | The backend stored the upload; graph indexing not yet confirmed |
| `indexing` | The document is listed in the dataset; the cognify pipeline is still running |
| `ready` | Listed and the pipeline is complete — recall can return it |
| `failed` | The backend rejected the write, or the pipeline reported an error |
| `unknown` | The write timed out or lost the connection, or the document never appeared within 15 minutes; an operator can `reconcile` or decide to resend |
| `deleted` | Removed by an operator and verified absent from the dataset |

A timed-out write is **not** retried blindly: its outcome is uncertain, so the receipt is
kept as `unknown` and later checks look for the document by its upload name. A 409 from
the backend is reported as a conflict for the operator to inspect, not retried.

`memory_status` and `lovable-memory reconcile` check `GET /datasets/status` and the
dataset listing; "accepted" is never presented as "searchable".

## Operator CLI

```
lovable-memory serve                      start the connector
lovable-memory health                     backend reachable? datasets present? ledger durable? (exit 1 on problems)
lovable-memory token                      print a fresh connector token
lovable-memory receipts [status]          list receipts (preview, status, data id)
lovable-memory status <receipt-id>        refresh one receipt against the backend
lovable-memory reconcile                  refresh every pending receipt
lovable-memory forget <receipt-id> --yes  delete one record on the backend, verify, clear it locally
lovable-memory config                     resolved configuration, secrets masked
```

**Deletion** uses cognee's targeted `forget` (`datasetId` + `dataId`), then re-lists the
dataset: only when the document is gone is the receipt marked `deleted` and its local
content cleared. The forget endpoint answers "success" even for an unknown id, so the
listing is the source of truth. Dataset-wide or account-wide deletion is deliberately not
available from this tool; use cognee directly for that.

**Changing a decision** in v0.1 means: `forget` the old receipt, then save the new
decision. Keeping both with version-aware retrieval is a later release.

## Security model

- Inbound: bearer token compared in constant time; Host allowlist (HTTP 421 otherwise);
  Origin allowlist when a browser-style Origin is present (403); request body cap (413);
  stateless MCP sessions so a replica or restart never strands a client.
- Outbound: one API key, sent only to `COGNEE_BASE_URL`; redirects are not followed, so
  the key cannot be bounced to another origin; responses are size-capped.
- Model inputs are schema-validated and truncated; unknown arguments (such as a
  `datasets` field) are stripped. Recall results are filtered to the configured datasets.
- Logs are JSON with token/key/authorization fields redacted and never include full
  record content. Tool results never include backend URLs or credentials.
- Dataset names organize storage; they are not an authorization boundary on the backend.
  Restrict the API key's permissions on the backend where supported, and run one
  deployment per isolation boundary.

## Library use

```ts
import { resolveConfig, MemoryService, createConnectorServer } from "@cognee/lovable-memory";

const config = resolveConfig({ projectDataset: "lovable_my_product" }); // env fills the rest
const service = new MemoryService({ config });
await service.prepare();
const { url } = await createConnectorServer({ config, service }).listen();
```

`MemoryService` (scope, receipts, status, verified deletion) has no MCP dependency; the
MCP handler in `src/server.ts` / `src/tools.ts` can be swapped for a supported official
preset later without touching it.

## Development

```bash
npm install
npm test            # build + unit, contract (fixtures from cognee 1.6.5) and MCP protocol tests
npm run typecheck
COGNEE_BASE_URL=http://127.0.0.1:8000 COGNEE_API_KEY=... npm run test:live   # disposable dataset on a real server
```

Compatibility details, verified backend behaviours and the "thin service vs cognee-mcp
preset" decision are in [`COMPATIBILITY.md`](COMPATIBILITY.md). Changes are in
[`CHANGELOG.md`](CHANGELOG.md).

## Application memory (follow-on)

The plan's second track — a Supabase-backed example app whose *users* get their own
memory through Edge Functions — is a separate release and is not part of this package. A
published Lovable app never includes the chat connector; it needs that backend path.
