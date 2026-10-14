# Controlled task outcomes

The operational report measures delivery and latency. This runner tests whether
memory changes task outcomes using held-out assertions and optional functional
tests. It compares `no_memory`, `cognee`, and any named candidates on matched
model, repository files, memory snapshot, random seed and token budget. Each
variant gets a fresh workspace, session ID and dataset name for every repetition.
Variant order is shuffled with a reproducible seed. Cloud recall remains **45s**.

Run the included offline protocol smoke suite (seven cases, three variants, three
repetitions = 63 trials). Use a new output directory for each run:

```bash
python3 "${PLUGIN_ROOT}/skills/cognee-report/scripts/plugin_eval.py" outcomes run \
  --suite "${PLUGIN_ROOT}/skills/cognee-report/fixtures/outcome-smoke.json" \
  --runner '["python3", "/absolute/plugin/path/skills/cognee-report/scripts/outcome_smoke_runner.py"]' \
  --repetitions 3 --out /tmp/cognee-outcomes-smoke
```

The smoke runner is deterministic and calls no model or Cognee server. Its scores
validate orchestration, scoring and reporting; they are **not evidence that Cognee
improves real tasks**. The fixtures cover interrupted tasks, superseded decisions,
exact symbols, paraphrases, irrelevant memory, stale indexes and cross-file impact.

For live trials, copy the suite, set `model` to a pinned model version, and replace
the smoke argv with the absolute path to `outcome_cognee_runner.py`. Set these
environment variables explicitly:

- `COGNEE_BASE_URL` and `COGNEE_API_KEY`: an evaluation server advertising
  `capture_idempotency_v1`; the current production dataset is never selected.
- `EVAL_MODEL_URL`: complete OpenAI-compatible chat-completions endpoint;
  `EVAL_MODEL_API_KEY`: its credential. Calls use temperature 0 and the paired seed.
- Optional `EVAL_INPUT_USD_PER_MILLION` and `EVAL_OUTPUT_USD_PER_MILLION`: documented
  model rates when the provider does not return cost. Rate-based cost is labeled.
- `EVAL_SERVER_USAGE_COMPLETE=true` only after verifying accounting coverage for
  the chosen server/provider paths. Otherwise combined cost/tokens remain unknown;
  measured subtotals and their basis remain in each trial record.

Live trials create `eval_*` datasets and incur model/server costs. Their memory
preparation uses idempotent typed capture and synchronous improve. Datasets remain
available for inspection; trial records identify their names. Recall runs in a
child process with a 45-second elapsed deadline, and late context is discarded.
The 9,000-character context cap and passage trimming are reused from the plugin.
The supplied HTTP adapter measures retrieval and answer claims, not coding-agent
tool use, warm digest behavior or multi-turn learning.

For coding tasks, provide an agent adapter using the same JSON protocol. It reads
one request on stdin and writes one JSON response on stdout. The request contains
the task prompt, workspace, isolated state directory, session/dataset, variant
config, model, seed, `max_tokens`, memory seed and recall budget. It never contains
the held-out expected answers. The adapter must use the provided state directory,
avoid global agent memory, honor the budget, and report the model actually used.

Each suite case has `id`, `prompt`, `memory: [{id,text}]`, and `expected`, for example:

```json
{"store": {"value": "postgres", "sources": ["current-decision"], "false_values": ["redis"]}}
```

State the required claim IDs in the task prompt. `files` optionally supplies a
relative-path-to-content mapping for a code fixture. `verify` is an argv array
run independently in that workspace after the adapter returns; nonzero exit fails
the task even if its answer claims success. A suite may also pin
`repository: {"path": "/local/checkout", "revision": "commit-sha"}`; each trial
gets a detached local clone, and the resolved commit enters the matching hash.
`variant_config` changes retrieval behavior (the live adapter accepts `scope` and
`top_k`); all other controls stay matched. Use a fixed task timeout for all arms.

Response example:

```json
{
  "model": "pinned-model", "session_id": "eval_trial", "dataset_name": "eval_trial",
  "claims": [{"id": "store", "value": "postgres", "citations": ["current-decision"]}],
  "retrieved_ids": ["current-decision"], "recall_ms": 1200,
  "usage_complete": true,
  "usage": [
    {"id": "answer", "source": "client", "tokens_in": 120, "tokens_out": 30,
     "cost_usd": 0.001, "token_basis": "provider_reported", "cost_basis": "provider_reported"},
    {"id": "session-total", "source": "server", "tokens_in": 400, "tokens_out": 80,
     "cost_usd": 0.003, "token_basis": "server_reported", "cost_basis": "server_estimated"}
  ]
}
```

Return one usage record per source-qualified operation. Exact duplicates are
deduplicated; conflicting duplicates and parent cycles invalidate usage. Set
`parent_id` and `includes_children=true` on inclusive totals to prevent counting
pipeline usage twice. Or use `--server-url` to replace adapter server records with
the unique trial session aggregate. Missing tokens or prices are unknown, not zero.

`trials.jsonl` and `report.json` contain outcome judgments and diagnostics, without
answer or memory text. The report includes sample counts, task success, citation
correctness and coverage, false-memory adoption, tokens/cost, whole-trial and recall
P50/P95 latency, per-case results, and paired wins/losses against no-memory and
current Cognee. Whole-trial latency and usage include cold memory preparation.
Timeouts and malformed results count as failures; missing pairs are reported and
excluded from paired comparisons. Citation correctness requires a correct claim,
a supporting oracle source, and evidence actually retrieved in that trial.
`false_values` identifies known incorrect facts seeded in the memory fixture;
their appearance in answers is an adoption proxy, compared against the no-memory
arm. Leave `sources` empty for claims requiring no memory evidence. An unrelated
memory citation must never receive correctness credit.

Rebuild a report without running models:

```bash
python3 "${PLUGIN_ROOT}/skills/cognee-report/scripts/plugin_eval.py" outcomes report \
  --trials /tmp/cognee-outcomes-smoke/trials.jsonl
```

Use repeated representative tasks and inspect case-level regressions before
changing defaults. These descriptive summaries do not claim statistical
significance or turn synthetic smoke results into product-quality evidence.
