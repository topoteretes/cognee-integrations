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
    isolation.py       # temp-HOME isolation: build_env / run_hook / load_suite_module
    identity_fake.py   # stateful auth / agent-session / dataset fake
    mock_cognee.py     # MockCogneeServer on pytest-httpserver, all routes
    payloads.py        # stdin payload builders for every hook event
    fixtures.py        # pytest fixtures wiring it all together
  tests/               # the actual test files (run by CI)
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

## Test-style guidance

- **Pure logic** (session ids, config layering, truncation, statusline
  rendering): plain in-process tests, no mock server.
- **HTTP boundary** (what request was sent / how a response or status code is
  handled): in-process via `isolated_modules` + `mock_server` — this exercises
  the real urllib stack over a socket, unlike hand-rolled `urlopen` fakes.
- **End-to-end** (a hook's full behavior): `run_hook` + `mock_server`.
- Connection-refused paths: point `COGNEE_BASE_URL` at an unbound port, not at
  the mock. Timeouts: a slow mock handler.
