<div align="center">
  <a href="https://www.cognee.ai">
    <img src="https://raw.githubusercontent.com/topoteretes/cognee-integrations/main/assets/cognee-logo.svg" alt="Cognee" width="260">
  </a>
  <p><strong>Cognee memory for Browser Use</strong> — a browser agent that reuses yesterday's research: bounded recall before a run, explicit memory actions during it, and a compact source-linked outcome saved afterwards.</p>
  <p>
    <a href="https://docs.cognee.ai">Docs</a> ·
    <a href="https://discord.gg/NQPKmU5CCg">Discord</a> ·
    <a href="https://github.com/topoteretes/cognee">Cognee core</a> ·
    <a href="https://github.com/browser-use/browser-use">Browser Use</a>
  </p>
</div>

# Cognee Memory Adapter for Browser Use

`cognee-integration-browser-use` composes around the open-source Browser Use Python
`Agent` (no subclass, no private patches) so repeated research gets cheaper without getting
stale:

- **One bounded recall before the run.** Up to 5 hits and 6,000 characters of evidence from
  earlier runs, with record names and observation times, injected through
  `Agent(extend_system_message=...)`, marked as historical and paired with the instruction
  to re-check changing facts on the current page. An 8-second deadline; failures degrade to
  an empty context and are recorded, never raised.
- **Explicit memory actions.** `cognee_recall` (and, opt-in, `cognee_remember`) registered
  into *your* `Tools` registry. Scope and dataset are bound in closures; the model only
  supplies a query or bounded content. Both actions are non-terminal and return actionable
  tool errors on failure.
- **Cheap step observation and a final outcome record.** Step hooks collect only step
  number, sanitized URL and action names. At the end the task, final result, outcome,
  sanitized source URLs, observation time and verification level are written as one text
  document to Cognee, with a receipt you can poll.
- **Cross-run persistence through a Cognee server** (self-hosted or Cloud) over its HTTP
  API. No in-process cognee, no graph database to install next to the agent.

What it deliberately does not do: archive screenshots or DOM, touch `sensitive_data`,
cookies or browser profiles, store model thoughts, extract a graph on every step, or replay
old element indexes. Memory stores observed outcomes and semantic procedures.

## Install

```bash
pip install cognee-integration-browser-use      # Python 3.11+, browser-use >=0.13.11,<0.14
export COGNEE_BASE_URL=http://localhost:8000     # or your Cognee Cloud tenant URL
export COGNEE_API_KEY=...                        # X-Api-Key for that server
```

You also need an LLM for Browser Use itself (for example `OPENAI_API_KEY`) and a browser
the way Browser Use normally finds one. The adapter adds no browser requirement.

## Use

```python
from browser_use import Agent, ChatOpenAI, Tools
from cognee_integration_browser_use import CogneeBrowserMemory, MemoryScope

async with CogneeBrowserMemory(receipts_path="./data/receipts.sqlite") as memory:
    run = memory.new_run(
        task=task,
        scope=MemoryScope(tenant_id="acme", user_id="procurement"),  # trusted, from your app
        query_hints=["supplier lead time"],
    )
    tools = Tools()
    run.register_tools(tools)  # cognee_recall (+ cognee_remember if enabled)
    context = await run.recall_context()  # bounded; degrades to empty on failure
    agent = Agent(
        task=task,
        llm=ChatOpenAI(model="gpt-4.1-mini"),
        tools=tools,
        extend_system_message=run.instructions(context),
    )
    history = await run.execute(agent, max_steps=30)  # the agent's own history
    print(run.status.to_dict())  # recall/capture status, receipts, tool calls
```

`run.execute()` composes your `on_step_start` / `on_step_end` callbacks with the adapter's
observer, runs `agent.run`, and captures the outcome in `finally`. It returns the original
history and re-raises the original exception or cancellation; memory problems are on
`run.status`, never in place of the browser result.

Applications that own the run loop can call `recall_context()`, `register_tools()`,
`observe_step(agent)` and `finalize(history, validator=..., summary=...)` individually
(`examples/custom_run_loop.py`). None of these alters an already-constructed agent's system
prompt: build the extension before constructing the `Agent`.

Runnable examples: [`examples/repeated_supplier_research.py`](examples/repeated_supplier_research.py)
(run it twice and watch the second run start with evidence) and
[`examples/custom_run_loop.py`](examples/custom_run_loop.py).

## Scope and records

Trusted application code supplies `MemoryScope(tenant_id, user_id, project_id)` (any
subset) or a single configured dataset. The adapter derives an opaque, stable dataset name
(`browser_use_<hash>`) from the scope; the ids never appear in it and the model never
chooses one. Dataset names organize storage; they are not an access-control boundary. Your
application authenticates its callers and the backend enforces its own credential
permissions. For strict per-site isolation, use separate scopes.

Each run leaves one record:

| Field | Purpose |
|---|---|
| `record_id`, `run_id`, `content_hash` | Deduplication and receipt tracking |
| `kind` | `research_finding`, `procedure`, `failed_attempt`, `interrupted_run`, `observation` (from `cognee_remember`), `preference` |
| `task`, `summary`, `outcome` | What was attempted and observed (size-capped, secrets redacted) |
| `source_urls`, `observed_at` | Provenance and freshness (userinfo and secret-looking query parameters stripped) |
| `verification` | `unverified` (default), `agent_reported` (agent said done+success), `application_validated` (your `validator` returned true) |
| `scope_key` | The authorized memory scope |

Recalled text is excluded from what gets captured, so memories do not recursively become
new observations. A cancelled run leaves a small `interrupted_run` record only when capture
is enabled and a bounded cleanup finishes in time.

## Receipts, status and deletion

Every write is recorded in a SQLite receipt store before the backend is called. With no
`receipts_path` the store is in memory and `run.status.durable` / `memory.health()` say so:
never treat in-memory mode as durable capture.

| Status | Meaning |
|---|---|
| `accepted` | The backend stored the upload; graph indexing not confirmed yet |
| `indexing` | Document listed; the cognify pipeline is still running |
| `ready` | Listed and the pipeline completed; recall can return it |
| `failed` | The backend rejected the write or the pipeline errored |
| `unknown` | The write timed out or lost the connection, or the document never appeared within 15 minutes |
| `deleted` | Removed by `forget_record()` and verified absent from the dataset listing |

```python
receipt = await memory.record_status(record_id)  # refreshes pending receipts
await memory.reconcile()  # all pending receipts
await memory.forget_record(record_id, scope=scope)  # targeted; verified by re-listing
```

`forget_record()` checks the receipt belongs to the given scope, deletes exactly that
document (`datasetId` + `dataId`), re-lists the dataset and only then marks the receipt
deleted. The backend's forget endpoint reports success even for unknown ids, so the listing
is the source of truth. No agent-facing forgetting exists; build a confirmation flow around
this method if users need one.

## Configuration

Precedence per field: constructor keyword → environment → default.

| Variable / keyword | Default | Purpose |
|---|---|---|
| `COGNEE_BASE_URL` / `base_url` | `http://localhost:8000` | Cognee server or Cloud tenant |
| `COGNEE_API_KEY` / `api_key` | required | Sent as `X-Api-Key` only to that URL; redirects are not followed |
| `COGNEE_BROWSER_USE_DATASET` / `dataset` | none | Fixed dataset when runs carry no scope |
| `COGNEE_BROWSER_USE_DATASET_PREFIX` / `dataset_prefix` | `browser_use_` | Prefix for scope-derived names |
| `COGNEE_BROWSER_USE_RECALL` / `recall_enabled` | `true` | Automatic recall before the run |
| `COGNEE_BROWSER_USE_RECALL_TOP_K` / `recall_top_k` | 5 | Hits injected |
| `COGNEE_BROWSER_USE_RECALL_MAX_CHARS` / `recall_max_chars` | 6000 | Evidence budget |
| `COGNEE_BROWSER_USE_RECALL_TIMEOUT_S` / `recall_timeout_s` | 8 | Automatic recall deadline |
| `COGNEE_BROWSER_USE_CAPTURE` / `capture_enabled` | `true` | Final-outcome capture |
| `COGNEE_BROWSER_USE_REMEMBER_TOOL` / `remember_tool_enabled` | `false` | Expose `cognee_remember` to the model |
| `COGNEE_BROWSER_USE_MAX_SUMMARY_CHARS` / `max_summary_chars` | 4000 | Cap on the stored summary |
| `COGNEE_BROWSER_USE_RECEIPTS_PATH` / `receipts_path` | in memory | SQLite receipt store; set it for restart recovery |
| `COGNEE_BROWSER_USE_REQUEST_TIMEOUT_S` / `COGNEE_BROWSER_USE_WRITE_TIMEOUT_S` | 15 / 30 | Backend deadlines |

Reads retry once within their deadline; writes are never retried automatically. A write
that times out stays `unknown` until a status check finds (or does not find) its document.

## Development

```bash
cd integrations/browser-use
uv sync --dev
uv run pytest tests/unit tests/contract -v          # no browser, LLM or server needed
COGNEE_LIVE=1 COGNEE_BASE_URL=... COGNEE_API_KEY=... uv run pytest tests/live -v
uv run python evals/run_comparison.py --fake        # evaluation harness plumbing
```

The contract tests replay bodies captured from cognee 1.6.5
([`tests/contract/fixtures/`](tests/contract/fixtures/README.md)). The live test captures
a run in one process, recalls it from a second process, checks that another scope cannot see
it, deletes it and recalls again. `evals/run_comparison.py` is a harness for the
three-condition comparison (no memory / saved summary / Cognee), not published results.

Versions, verified behaviours and judgement calls are in
[`COMPATIBILITY.md`](COMPATIBILITY.md); changes in [`CHANGELOG.md`](CHANGELOG.md).
