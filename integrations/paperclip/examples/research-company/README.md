# Example: research → product decision → marketing brief

`company.json` holds a small Paperclip company whose three agents hand work to
each other through company memory:

| Task | Project | What the plugin does |
|---|---|---|
| RES-1 (done) | Customer research | Captured automatically as a completed task; the `#remember` line in its description is also a marked decision once posted as a comment |
| PROD-7 (done) | Product | Captured automatically; its `#remember` comment is captured as a recorded decision |
| MKT-3 (todo) | Marketing | A fresh run calls `cognee_recall` and finds both RES-1 and PROD-7 with links, then writes the brief and records its conclusion with `cognee_remember_decision` |

## Plugin configuration for this company

```json
{
  "cogneeBaseUrl": "https://your-tenant.aws.cognee.ai",
  "cogneeApiKeyRef": { "type": "secret_ref", "secretId": "<id of the secret holding the cognee API key>" },
  "enabledCompanyIds": ["0d9f2a4e-7c1b-4b6e-9a3f-5e2d8c1a7b10"],
  "enabledProjectIds": [
    "6a1c3b2d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    "7b2d4c3e-5f6a-4b7c-9d8e-0f1a2b3c4d5e",
    "8c3e5d4f-6a7b-4c8d-ae9f-1a2b3c4d5e6f"
  ],
  "companySharedDataset": "northwind_shared",
  "captureMode": "both",
  "decisionMarker": "#remember",
  "paperclipPublicUrl": "https://paperclip.example.com"
}
```

With three projects enabled, each project writes to its own dataset
(`pc_<hash>`), and every project's agents can also read `northwind_shared`.
For the marketing run to see research and product records across projects in
this example, either enable the shared dataset and copy the two records there,
or (simplest for a demo) put all three tasks in one project. The plugin never
widens a project's read scope on its own.

## Running it against a dev host

1. Create the company, projects and agents in your Paperclip instance (UI or
   API) using the names above; replace the ids in `company.json` and in the
   configuration with the ones your instance assigned.
2. Install and configure the plugin (`npx paperclipai plugin install
   @cognee/paperclip-plugin`), then save the configuration.
3. Create RES-1 and PROD-7 and mark them **done**; post the `#remember` comment
   on PROD-7. Each event queues a ledger row and the write is sent immediately;
   the `drain-outbox` job retries anything the backend did not accept.
4. Wait for `memory-health` (plugin data key) to report the rows as `ready`,
   or run the `reconcile-sources` job manually.
5. Create MKT-3 and run the Marketer agent. The company-memory skill tells it
   to call `cognee_recall` first and `cognee_remember_decision` afterwards.

## Running it in the SDK test harness

`tests/live/live.test.mjs` seeds the same shapes into `createTestHarness`,
emits `issue.updated` for a done issue, drains the outbox against a real
cognee server, polls readiness and recalls through `cognee_recall`. Run it
with `COGNEE_LIVE=1 COGNEE_BASE_URL=... COGNEE_API_KEY=... npm run test:live`.
