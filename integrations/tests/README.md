# Shared test infrastructure — Claude Code + Codex

Reusable pytest harness for the two Python hook suites, `claude-code` and
`codex`, which are the same code differing only in constants. One parametrized
test set runs against both. openclaw (TypeScript) and hermes-agent (SDK-based)
are out of scope here.

## Layout

```
integrations/tests/
  conftest.py          # sys.path bootstrap + registers utils.fixtures as a plugin
  utils/
    suites.py          # Suite descriptors (per-suite constants) + dir helpers
    isolation.py       # temp-HOME isolation: build_env / run_hook / module loaders
    identity_fake.py   # stateful auth / agent-session / dataset fake
    mock_cognee.py     # MockCogneeServer on pytest-httpserver, all routes
    payloads.py        # stdin payload builders for every hook event
    statusline.py      # suite-aware status-line expectations (styled vs plain)
    fixtures.py        # pytest fixtures wiring it all together
  tests/
    unit/              # in-process, no mock server (isolated_modules/hook_module)
    integration/       # in-process code exercising the mock server over real HTTP
    e2e/               # hook scripts run as subprocesses against the mock server
```

Run locally from this directory:

```
uv sync --dev
uv run pytest tests/ -v
```

CI runs the same via `.github/workflows/ci.yml`; changes under
`integrations/claude-code/`, `integrations/codex/`, or `integrations/tests/`
all trigger this suite.

## Ground truth (verified against the scripts)

| | `claude-code` | `codex` |
|---|---|---|
| Scripts dir | `claude-code/scripts/` | `codex/plugins/cognee/scripts/` |
| config.json | `~/.cognee-plugin/claude-code/` | `~/.cognee-plugin/` (shared root) |
| State dir | `~/.cognee-plugin/claude-code/` | `~/.cognee-plugin/codex/` |
| Default dataset | `agent_sessions` | `agent_sessions` |
| `agent_name` default | `claude-code-agent` | `codex-agent` |
| `session_prefix` | `claude` | `codex` |
| cwd env var | `CLAUDE_CWD` | `CODEX_CWD` |
| Agent-session suffix | `_claude` | `_codex` |
| `has_background_remember` | `True` | `False` |

`Suite.has_background_remember` gates the one large **intentional** divergence:
claude-code has the background-remember + cognify-poll refactor (writes post
`run_in_background=true`, `_post_remember_document` returns an `{"ok": ...}`
envelope instead of raising, `wait_for_cognify` exists, `_remember_http` honours
a bounded wait, and improve polls cognify/memify). codex still has the older
synchronous, raise-on-error path, so tests for those behaviours skip on codex.

When a divergence is an **unintentional gap** rather than a design difference,
gate it with a *strict* conditional `xfail` marker, never `pytest.skip` and never
an imperative `pytest.xfail()` call:

```python
if suite.name == "codex":
    request.node.add_marker(pytest.mark.xfail(reason="...", strict=True))
```

The imperative form aborts the test body, so it can never report XPASS and would
keep silently not-testing the suite forever once the bug is fixed. With
`strict=True` the body still runs and the fix surfaces as an `XPASS(strict)`
failure — the signal to delete the gate. That is how the surrogate-sanitization
gap (fixed in #334) closed itself; there are no such gates open today.

### Status line: styled (claude-code) vs plain text (codex)

The renderers share their resolution logic — which glyph wins, marker
attribution and freshness, the base_url mismatch guard, path containment — but
claude-code styles the bar with ANSI for a terminal while codex's string is
injected into the model's context and must stay plain. Shared tests therefore
assert through `utils.statusline` (`ok_glyph`, `fail_glyph`, `mode_label`,
`strip_ansi`); the styling rules and the plain-text guards live in per-suite
sections that skip on the other suite. Segments codex simply does not have
(`_mode_label`, `_recall_segment`, `_pipeline_health_glyph`) are gated on the
function's presence, as is claude-code's install registry
(`_INSTALLED_PLUGINS_PATH`, which codex has no equivalent of).

Marker paths all derive from `Path.home()`, so the `statusline` fixture needs no
path patching — tests write real marker files at the module's own constants
inside the per-test HOME.

Shared facts that shape the harness:

- **Base URL env var is `COGNEE_BASE_URL`** (`config.py` `_ENV_MAP`). An empty
  `base_url` routes to local-SDK mode — tests that want the mock server MUST
  pass `service_url=mock_server.url` or the hook will try to boot a local
  server instead.
- **Dir constants are import-time**, resolved from `$HOME`/`USERPROFILE`:
  `~/.cognee-plugin[/...]` (config/state) and `~/.cognee` (local-SDK data,
  `.env`). Isolation = set HOME before the child imports (subprocess) or
  import the module after setting HOME (in-process). `XDG_CONFIG_HOME` is not
  read.
- **Identity is a single principal key** (session-start
  `_resolve_single_principal_key`): env `COGNEE_API_KEY` → cached key →
  `POST /auth/login` (form) → `GET /auth/api-keys` (cookie, reuse `keys[0]`)
  → `POST /auth/api-keys` (mint). The legacy per-agent bootstrap
  (`auth/register`, `agents/create` + 409→list→delete→retry) was removed from
  the runtime and is not faked.
- **Determinism knobs** (set by `build_env` by default):
  `COGNEE_IDLE_DISABLED=1` (no idle/exit watcher spawns),
  `COGNEE_UPDATE_CHECK=off` (no raw.githubusercontent call),
  `COGNEE_LAZY_BOOTSTRAP=0` (SessionStart bootstraps synchronously instead of
  via a detached worker that can outlive the test). `build_env` also mirrors
  the mock URL into `COGNEE_PLATFORM_API_URL` so billing calls can't escape.

### Hook event → script map (identical events, per-suite paths)

| Event | Script (+ args) |
|---|---|
| SessionStart | `session-start.py` |
| UserPromptSubmit | `session-context-lookup.py`, then `store-user-prompt.py` |
| PostToolUse | `store-to-session.py` |
| Stop | `store-to-session.py --stop`, `credits-refresh.py` (+ claude: `clear-transcript-context.py`) |
| PreCompact | `pre-compact.py` |
| SessionEnd | `sync-session-to-graph.py --session-end` |

### Endpoints the mock serves

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | configurable via `set_health_status` |
| GET | `/docs` | reachability probe (`_backend_reachable`) |
| POST | `/api/v1/auth/login` | form `{username,password}` → `{access_token: <jwt>}` |
| GET/POST | `/api/v1/auth/api-keys` | cookie `auth_token`; list `[{key}]` / mint `{key}` |
| GET | `/api/v1/users/me` | `X-Api-Key` probe → `{id}` or 401 |
| POST | `/api/v1/agents/register` | `{agent_session_name, type, memory_mode, source, ...}` |
| POST | `/api/v1/agents/unregister` | → `{activeAgents}` |
| GET | `/api/v1/agents/connections/me` | → `{agent: {agent_session_name, user_id, tenant_id, status}}` |
| POST | `/api/v1/remember` | multipart `data`/`datasetName`/`node_set`/`run_in_background` → `{dataset_id, pipeline_run_id}` |
| POST | `/api/v1/remember/entry` | `{entry, dataset_name, session_id}` → `{entry_id}` |
| POST | `/api/v1/recall` | → top-level JSON **array** (`set_recall_results`) |
| POST | `/api/v1/improve` | `{dataset_name, session_ids, run_in_background}` → `{dataset_id, status}`; `{}` = busy (`set_improve_response`) |
| POST | `/api/v1/datasets` | idempotent ensure → 201/200 `{id, name}` |
| GET | `/api/v1/datasets/status` | `?dataset=<id>&pipeline=` → `{<id>: <STATUS>}` (`set_dataset_status`) |
| GET | `/api/v1/billing/credits/overview` | platform API; `{tenants: [{tenantId, remainingUsd, spentUsd, maxBudgetUsd}]}` (`set_credits_overview`) |

Assert traffic with `mock_server.assert_called(method, path, **json_fields)`
(subset match — never deep-equal a whole body; async hooks may emit extra
calls) and `assert_not_called`.

## The live tier (`e2e/live/`)

Same driver as the rest of `e2e/` — hook scripts as subprocesses — but pointed at
a **real cognee server that the plugin boots itself**, with real LLM calls and a
real graph. It exists because a hermetic e2e can only prove "the hook ran and
produced well-formed output": with a mock there is no graph, so nothing about
memory actually working is testable.

```bash
COGNEE_RUN_LIVE=1 LLM_API_KEY=sk-... uv run pytest tests/e2e/live -m live
```

Needs no Claude account, no agent CLI and no `ANTHROPIC_API_KEY`: the host is
simulated by invoking the hooks directly. What that gives up — *would the host
actually call these hooks* — is covered hermetically by
`unit/test_hooks_contract.py`, which asserts `hooks.json` wires every event the
live driver simulates, to a script that exists, with the same args.

Assertions come in two deterministic layers, so a failure says *where* the chain
broke:

- **L1** the graph itself holds the memory (direct `POST /api/v1/recall`)
- **L2** the plugin *injects* it into a fresh session's prompt — the actual
  product contract

There is no "the model said it" layer: no model runs here. Every memory
assertion hangs off a per-test **nonce** (`ZEPHYR-xxxxxx`) that cannot exist in
training data, so a pass is evidence rather than coincidence.

Non-obvious rules this tier encodes (each one learned by getting it wrong —
`utils/live.py`'s docstring has the details):

- **`COGNEE_BASE_URL`** is what selects server mode. Setting only
  `COGNEE_LOCAL_API_URL` leaves `base_url` empty, so the plugin runs local-SDK
  mode, boots nothing, and every hook still exits 0 — a test that passes while
  exercising the wrong backend. `started_session` asserts a server really is
  listening.
- **Never the default port 8011** — a developer's own cognee usually owns it, and
  a live test must never write into a real graph. Ports are ephemeral and pinned.
- **`improve_fired` is not a completion signal**: it can report `ok: true` with
  empty `cognify`/`memify` while the graph *was* written. The readiness gate is
  "poll recall until the content comes back".
- **The venv is seeded** from the host's `~/.cognee-plugin/venv` so boot is ~15s
  instead of a multi-minute `uv` install. That caches the *install* only.
- **Recall timeouts are raised** (`COGNEE_RECALL_TIMEOUT`/`_BUDGET`). Production
  keeps them tight (2.5s/4s) so memory can never stall an interactive prompt, and
  a cold server's first graph query correctly exceeds that. These tests ask
  whether memory crosses sessions, not whether cold-start recall is fast — so
  cold-start deserves its own scenario rather than silently failing this one.

Failures dump `hook.log`, the recall-related events, `recall-audit.log` and
`last_recall.json` automatically; a live failure without them is unactionable.

### Scenarios

| File | Covers | Cognify? |
|---|---|---|
| `test_cross_session_recall.py` | the core promise: two turns in session A are recalled by a fresh session B on the same dataset | yes |
| `test_session_capture.py` | within-session recall of prompts, answers and tool traces, plus the save counters behind the status line | no — the session cache answers directly, so these are the cheap ones |
| `test_user_surfaces.py` | the three places a user meets memory: the status line (rendering for both, counts for claude-code), the pre-compact anchor (**strict xfail on claude-code only**, see below), and `cognee-search.sh` | no |
| `test_resilience.py` | hooks stay successful when the server dies; a mid-outage write should be buffered (**strict xfail**, see below); buffered turns replay once the server returns; a slow cold query is judged `slow`, not `down`, and never trips the breaker | one |
| `test_concurrency.py` | two sessions' interleaved turns both reach the graph, and each session claims its own final sync | one |
| `test_graph_writes.py` | two populated datasets do not leak into each other; a repeated SessionEnd starts exactly one final-sync worker | two |
| `test_shared_brain.py` | either integration recalls what the other wrote to the shared graph, **in both directions** — only testable live, and cheap now that no CLI is involved | yes |

### Both integrations, every scenario

`live_suite` is parametrized over `ALL_SUITES`, so all 17 scenarios run twice —
34 suite-parametrized tests plus the 2 shared-brain directions. That doubles
wall-clock and LLM spend deliberately: the two plugins diverge precisely where a
mock cannot show it. codex's bridge is synchronous with no cognify poll
(`has_background_remember=False`), so codex-as-writer reaches the graph by a
different path, and only a real graph can tell you whether it arrives.

Three fixtures are deliberately **suite-agnostic**, which is what makes the
cross-suite direction possible:

- `live_home` — the venv and `~/.cognee` are shared; each suite keeps its own
  state subdirectory beneath it.
- `graph` / `GraphClient` — the principal key is minted once per HOME at the
  shared `~/.cognee-plugin` root, and the graph is scoped by dataset, not by
  plugin. (`GraphClient.suite` existed but was never read; it is gone.)
- `live_artifacts` — as an **autouse** fixture, depending on `live_suite` would
  force the parametrization onto every test in the directory, including the
  cross-suite ones. It iterates the suites instead, which also means a cross-suite
  failure dumps both sides.

Cross-suite tests use `session_for(suite, name)` rather than `started_session`:
the latter rides the parametrization, so a writer/reader pair built from it would
be the same integration on both sides.

Whole tier: **32 passed, 1 skipped, 3 xfailed in ~24m30s** (the skip is codex's
counts segment; the xfails are the gaps below). Roughly 3x the single-suite time
rather than 2x, because each suite boots its own server per test.

Two behaviours worth knowing before writing more of these, both learned by
getting them wrong:

- **The warmup buffer is per session.** `_bridge_file(session_id)` means a session
  replays only what it buffered, so another session's prompt can never drain a
  stranded backlog. Recovery is driven by the session that buffered it.
- **The drain rides on the prompt hook.** Polling the buffer file while nobody
  prompts will never see it move, and a drain that failed during an outage arms a
  ~60s backoff worth prompting past.

Waiting on a repeated step needs `wait_for_event_count`, not `wait_for_event`:
the latter returns immediately on a previous round's entry, which is how a test
ends up asserting state from before the action it just performed.

Assertions prefer the per-scope hit counts in `last_recall.json` over the prose a
semantic search returns: "the trace scope found something" is a structural fact,
while which sentence comes back is not something the plugin controls.

### Open gaps this tier found

Both are **strict xfails** recording real defects, not intended differences, so
each turns red the moment it is fixed. They share a shape: the plugin is careful
never to break the agent, and the cost is that these failures are *silent* — a log
line and carry on.

**1. A mid-session outage can lose a turn — both integrations.**
`test_writes_during_an_outage_are_buffered_not_dropped`. `store-to-session.py`
buffers to the warmup spillway only when `server_usable()` is already False (a
*stale* ready marker plus a failed probe). The marker has a 30s TTL, so a server
that dies inside that window leaves `server_usable()` returning True: the hook
attempts a real write, it raises, and the `except` branch only logs
`stop_store_error` — the entry is buffered nowhere and that turn is lost, which is
exactly what the spillway exists to prevent. Fix: call `append_warmup_entry` in
that `except` branch, as the not-usable path already does. The two suites are
structurally identical here, so this xfails on both.

**2. PreCompact produces no anchor in server mode — claude-code only.**
`test_precompact_produces_an_anchor_carrying_the_session`. Running this against
both integrations changed the diagnosis: this is not a missing feature, it is a
**port that never happened**. codex's `pre-compact.py` branches on
`is_cloud_mode` and recalls via `recall_via_http`; claude-code's recalls via
`cognee.recall` with a `get_session_manager()` fallback, both local-SDK only, while
in server mode the session cache lives on the server. So on claude-code the session
and trace entries come back empty, the derived query stays empty, the graph scopes
are never queried, and it logs `precompact_empty` and prints nothing.

`is_cloud_mode` is just `bool(base_url)`, so a loopback server counts and codex
takes the HTTP path everywhere this tier runs. The effect on claude-code is that
anyone running against a server loses their anchor at exactly the moment compaction
discards the transcript. The fix does not need designing — it exists in the sibling
integration. Gated on `has_precompact_http`, so it xfails strictly on claude-code
and must **pass** on codex.

### A third gap, not yet covered by a test

**A session stranded at SessionEnd never replays.** Surfaced while fixing
`test_buffered_turns_are_replayed_once_the_server_returns`: because the warmup
buffer is keyed per session, a session whose server is still down when it ends
leaves its buffered turns on disk with nothing scheduled to replay them — no other
session can drain a session-keyed buffer. Recorded here rather than as an xfail
because it needs a product decision (who owns an orphaned buffer?) rather than a
one-line fix.

## The per-integration test dirs are gone

`claude-code/tests/`, `codex/tests/` and `codex/plugins/cognee/tests/` no longer
exist; everything they held now lives here. What made this worth finishing rather
than leaving as accepted duplication is that those eight files were not duplicates
at all:

- `plugin-windows-tests.yml` invoked five of them directly as `python <file>`, so
  they ran **only on Windows** and never on Linux or macOS;
- `ci.yml` only ever runs `pytest tests/` (this suite), so the other three —
  `test_hook_timing.py`, `test_per_scope_timing.py`,
  `test_recall_health_accounting.py` — ran in **no CI job on any platform**.

Where each landed:

| Was | Now | Notes |
| --- | --- | --- |
| `{claude-code,codex}/tests/test_proc.py` | `unit/test_proc_helpers.py` | `_proc.py` is byte-identical across suites, so parametrizing it is a drift guard; the Toolhelp path is a real `skipif`, not a silent early `return` |
| `{claude-code,codex}/tests/test_statusline_render.py` | `e2e/test_statusline_encoding.py` | cp1252 regression; asserts **bytes**, since decoding in the parent would hide what the child actually wrote |
| `claude-code/tests/test_hook_timing.py` | `unit/test_timing_metrics.py` | claude-only via `has_timing_metrics` |
| `claude-code/tests/test_per_scope_timing.py` | `unit/test_recall_per_scope.py` | applies to **both** suites — codex has the same `per_scope`/budget machinery |
| `claude-code/tests/test_recall_health_accounting.py` | `unit/test_recall_health_accounting.py` | both suites; seams are seam-for-seam identical |
| `codex/plugins/cognee/tests/test_doctor.py` | already covered | every one of its 25 cases has a counterpart in `unit/test_doctor_resolution.py` or `integration/test_doctor.py` |

The two recall files share `utils/recall.py`, which drives
`session-context-lookup._run` in cloud mode and captures every seam at once
(events, connection-state writes, breaker calls, dispatched scopes and their
timeouts). Its stubs use `monkeypatch.setattr` at the default `raising=True`, so
a seam renamed in one integration and not the other fails loudly rather than
quietly testing nothing.

### Windows

`plugin-windows-tests.yml` now runs this suite on `windows-latest` instead of
five hand-listed scripts, which means any test added here is checked on Windows
automatically. Isolation works there because `build_env` and `_isolate_process_env`
set `USERPROFILE` alongside `HOME` (`Path.home()` reads the former on Windows),
and the handful of tests that encode POSIX file-mode semantics skip themselves on
`os.name == "nt"`.

**Unverified:** the suite has never actually executed on Windows — it was
statically audited, not run. The first run of that workflow is the real check.

## Fixture API

- `suite` — parametrized `Suite`; every test using it runs once per suite.
- `temp_home` / `project_dir` — per-test dirs; nothing touches the real `~`.
- `mock_server` — running `MockCogneeServer`; `mock_server.url` goes into
  `COGNEE_BASE_URL`. The default `run_hook` API key (`test-api-key`) is
  pre-seeded as valid.
- `run_hook(suite, script, *args, stdin=..., service_url=...)` — e2e entry
  point: runs `python3 <suite_scripts_dir>/<script>` with isolated HOME;
  `stdin` may be a dict. Returns `subprocess.CompletedProcess`.
- `isolated_modules(suite, name)` — unit entry point: fresh-imports one of
  `config`, `_plugin_common`, `_cognee_client`, `_env_file`, `_proc`,
  `_recall_http`, `_remember_http` under the temp HOME.
- `payloads` — builders `session_start / user_prompt / post_tool_use / stop /
  session_end`, each taking `**overrides`.
- `assert_clean_real_home` — guard fixture asserting the real
  `~/.cognee-plugin` is untouched.

## Test-style guidance — which tier?

- **`unit/`** — pure logic and local state (session ids, config layering,
  truncation, locks, dedup bookkeeping, statusline rendering) plus the
  exception taxonomies a server cannot produce (DNS failure, SSL handshake,
  connection reset). Use `isolated_modules` / `hook_module`; no mock server.
- **`integration/`** — the HTTP boundary: what request actually went on the
  wire, and how a real response or status code is handled. In-process code via
  `isolated_modules` pointed at `mock_server`, which exercises the real urllib
  stack over a socket unlike a hand-rolled `urlopen` fake. Assert with
  `assert_called(method, path, **fields)`.
- **`e2e/`** — a hook script's full behavior via `run_hook` + `mock_server`,
  including what it leaves on disk under the temp HOME.

Rule of thumb: if the assertion is about "what request did we send" or "how do
we react to what the server returned", it belongs in `integration/` even when
it reads like a unit test. If it is about local computation or state, `unit/`.

Practical notes:

- **Connection refused**: use the `closed_port_url` fixture (a genuinely
  unbound port) — the mock server cannot express "server absent". Timeouts: a
  slow mock handler or a short client deadline.
- **Malformed bodies**: `force_response(..., body=b"raw bytes")` sends the body
  verbatim so it can be invalid JSON.
- **Sequences**: `set_dataset_status([...])` and `set_credits_overview([...])`
  walk one entry per request (last sticks); an `int` entry answers with that
  HTTP status, which is how a transient mid-poll failure is expressed.
- **Speed**: the HTTPServers are session-scoped and reset per test by
  `mock_server` / `platform_server` (stopping a server costs ~0.5s, which at
  this test count dominated the run). Never hold a reference to a mock across
  tests.
