# cognee-integration-backend-core

The standardized cognee access layer for integrations in this monorepo: one
contract, three interchangeable backends, and the hardened runtime posture for
running cognee in-process.

```python
from cognee_backend_core import (
    LocalCogneeAdapter,  # cognee in this process (fast single-user engine)
    HttpCogneeAdapter,  # a cognee server or cognee cloud over HTTP
    FakeAdapter,  # offline substring stand-in for tests and demos
    single_user_runtime,  # env posture — call before `import cognee`
)

single_user_runtime("~/.myapp/cognee", _i_am_single_tenant=True)  # local, ONE user only
adapter = LocalCogneeAdapter("my_dataset")
await adapter.add(["/path/doc.md"])
await adapter.cognify()
hits = await adapter.chunks("what the doc means")  # semantic passages
reply = await adapter.answer("a question")  # graph answer
```

`HttpCogneeAdapter` additionally exposes the chat-memory surface —
`remember(text, node_set=…)`, `recall(query)`, `forget()` — with the error
semantics proven out in the Claude Code plugin: a missing dataset (4xx) is
"no results", never an error; 5xx / connection failures propagate so callers
can tell "empty" apart from "backend down".

## The runtime posture (read this before embedding cognee)

`single_user_runtime()` encodes lessons that cost real debugging time:

| Setting | Why |
|---|---|
| `*_ROOT_DIRECTORY` forced | Sharing a store with another cognee install fails at migration time; dev shells often export these globally |
| `CACHING=false` | Session memory rewrites every query through an LLM before retrieval — seconds per interactive search |
| `ENABLE_BACKEND_ACCESS_CONTROL=false` | Multi-tenant mode spawns and tears down a DB worker per query (~4s); single-user keeps one warm engine (~0.25s) |

Two rules that follow: the posture must match at **index** time and **search**
time (data written under one is invisible under the other), and cognify must
run **in the same process** as the warm engine (a separate process fights it
for the store lock).

## Who uses it / migration map

| Integration | Status |
|---|---|
| `spotlight` | ✅ built on this package (`spotlight_backend.adapters` is a thin re-export) |
| `second-brain` | candidate: its `CogneeHttpClient` ≈ `HttpCogneeAdapter.remember/recall/forget` |
| `chat-memory` | candidate: same HTTP surface |
| `claude-code` | hook scripts must stay stdlib-only (no venv at hook runtime); its wire contract is mirrored here, not imported |

Add as a path dependency:

```toml
dependencies = ["cognee-integration-backend-core"]
[tool.uv.sources]
cognee-integration-backend-core = { path = "../backend-core", editable = true }
```

## Tests

```bash
uv sync && uv run pytest
```
