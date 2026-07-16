---
name: cognee-falkor-setup
description: Use when an agent must stand up the OpenClaw↔Cognee integration with FalkorDB as the vector + graph store. This skill is SELF-CONTAINED — it tells you to generate every file (Dockerfile, sitecustomize.py, docker-compose.yaml) from the exact contents below, build a custom Cognee image that loads the FalkorDB adapter, run Cognee + FalkorDB together, self-verify the adapter registered, and point the OpenClaw cognee plugin at it (including per-agent graphs). No other files in this directory are required.
---

# Cognee + FalkorDB setup for OpenClaw (self-contained)

You (the agent) will **generate** the files below, build a custom Cognee image,
run it alongside FalkorDB, verify the FalkorDB adapter is active, then configure
the OpenClaw `cognee-openclaw` plugin. Follow the steps in order. Each major step
has a **verify** check — if a check fails, follow its "if it fails" note before
continuing. Do not skip verification.

## Preconditions (fail loudly if unmet)

- You can **write files** in a working directory and **run Docker** (`docker`, `docker compose`).
- You have an **LLM API key** (OpenAI by default) for Cognee's entity extraction.
- Pick a working directory, e.g. the directory containing this skill. All paths below are relative to it.

If you cannot write files or run Docker, stop and report that — this skill cannot proceed.

## Why this construction (read once)

The stock `cognee/cognee` image does **not** ship the FalkorDB adapter, and there
is **no env var** that turns it on. Cognee's `falkor` provider only becomes
resolvable after `cognee_community_hybrid_adapter_falkor.register` is imported —
that import registers the `falkor` graph + vector providers and the
`falkor_graph_local` / `falkor_vector_local` per-dataset handlers. So we:

1. install the adapter into Cognee's venv, and
2. drop a `sitecustomize.py` on the image's `PYTHONPATH` (`/app`). Python
   auto-imports `sitecustomize` at interpreter startup for **every** process —
   the gunicorn master, each worker, and the alembic migration — so `register`
   runs before any DB engine is created, **without editing Cognee's entrypoint**.

This is deliberately the smallest possible change (no custom entrypoint, no app
wrapper) so it stays correct across Cognee patch releases.

## Step 1 — Generate the files

Create these three files **verbatim** in your working directory.

### `sitecustomize.py`

```python
# Auto-imported by Python on startup because it sits on PYTHONPATH=/app in the
# cognee image. Importing the adapter's `register` module here makes the
# "falkor" graph + vector providers and the falkor_*_local per-dataset handlers
# resolvable BEFORE Cognee creates any engine — no entrypoint/app edits needed.
import cognee_community_hybrid_adapter_falkor.register  # noqa: F401
```

### `Dockerfile`

```dockerfile
# Custom Cognee image with the FalkorDB hybrid adapter registered.
# Keeps the stock entrypoint and stock app (cognee.api.client:app); the adapter
# is activated by sitecustomize.py (auto-imported via PYTHONPATH=/app).
FROM cognee/cognee:1.1.0

# uv installs into the uv-created venv (which has no pip). --no-deps on the
# adapter avoids re-resolving the already-installed cognee; its other deps
# (starlette, instructor) are already present. Install the falkordb client too.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN uv pip install --python /app/.venv/bin/python --no-deps "cognee-community-hybrid-adapter-falkor==0.3.0" \
 && uv pip install --python /app/.venv/bin/python "falkordb>=1.0.9,<2.0.0"

# Auto-imported on every interpreter start -> registers the FalkorDB adapter.
COPY sitecustomize.py /app/sitecustomize.py

# Use FalkorDB for BOTH vector and graph, with per-dataset isolation (each
# dataset -> its own FalkorDB graph keyed by dataset_id) and multi-user access
# control. URLs point at the "falkordb" compose service; override via env if you
# rename it or run Cognee outside compose.
ENV GRAPH_DATABASE_PROVIDER=falkor \
    GRAPH_DATABASE_URL=falkordb \
    GRAPH_DATABASE_PORT=6379 \
    VECTOR_DB_PROVIDER=falkor \
    VECTOR_DB_URL=falkordb \
    VECTOR_DB_PORT=6379 \
    GRAPH_DATASET_DATABASE_HANDLER=falkor_graph_local \
    VECTOR_DATASET_DATABASE_HANDLER=falkor_vector_local \
    ENABLE_BACKEND_ACCESS_CONTROL=True
```

> Version pins: `cognee/cognee:1.1.0` matches this repo. The adapter is pinned to
> `0.3.0`; the adapter supports `cognee >=1.0.3,<2.0.0`. If the build fails on the
> adapter install, check the latest compatible version on PyPI
> (`cognee-community-hybrid-adapter-falkor`) and update the pin — do **not** drop
> the pin entirely.

### `docker-compose.yaml`

```yaml
services:
  falkordb:
    image: falkordb/falkordb:latest
    container_name: falkordb
    ports:
      - "127.0.0.1:6379:6379"   # FalkorDB/Redis protocol
      - "127.0.0.1:3001:3000"   # FalkorDB browser UI (optional)
    volumes:
      - falkordb_data:/data
    restart: unless-stopped
    healthcheck:
      test: [ "CMD", "redis-cli", "ping" ]
      interval: 10s
      timeout: 5s
      retries: 5

  cognee:
    build:
      context: ..
      dockerfile: Dockerfile
    image: cognee-falkor:1.1.0
    container_name: cognee
    depends_on:
      falkordb:
        condition: service_healthy
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      - LLM_API_KEY=${LLM_API_KEY}
      - AUTO_FEEDBACK=true
    volumes:
      - cognee_data:/app/cognee/.cognee_system
    restart: unless-stopped
    healthcheck:
      test: [ "CMD", "curl", "-f", "http://localhost:8000/health" ]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s

volumes:
  falkordb_data:
  cognee_data:
```

**Verify (files):** `ls sitecustomize.py Dockerfile docker-compose.yaml` lists all three.

## Step 2 — Build and run

```bash
export LLM_API_KEY="sk-..."          # required; or put LLM_API_KEY=... in a .env file here
docker compose up -d --build
```

**Verify (build & containers):**
```bash
docker compose ps          # both 'falkordb' and 'cognee' present
```
*If the build failed on the adapter install:* re-read the Dockerfile version pin
note above, adjust the adapter version, and rerun `docker compose up -d --build`.

## Step 3 — Verify the FalkorDB adapter actually registered (critical)

This is the check that distinguishes a working Falkor backend from a silently
broken one. Wait for health, then confirm the providers resolve **inside the
container**:

```bash
# 1. API healthy (retry for ~60s on first boot while migrations run)
until curl -fsS http://localhost:8000/health >/dev/null; do sleep 3; done; echo "health OK"

# 2. The 'falkor' providers + per-dataset handlers are registered
docker exec cognee /app/.venv/bin/python -c "import sitecustomize; \
from cognee.infrastructure.databases.graph.supported_databases import supported_databases as g; \
from cognee.infrastructure.databases.vector.supported_databases import supported_databases as v; \
from cognee.infrastructure.databases.dataset_database_handler.supported_dataset_database_handlers import supported_dataset_database_handlers as h; \
print('graph falkor:', 'falkor' in g); print('vector falkor:', 'falkor' in v); \
print('handlers:', [k for k in h if 'falkor' in k])"

# 3. FalkorDB reachable
docker exec falkordb redis-cli PING        # -> PONG
```

**Expected:** `graph falkor: True`, `vector falkor: True`,
`handlers: ['falkor_graph_local', 'falkor_vector_local']`, and `PONG`.

*If `graph falkor: False`:* the adapter didn't register. Confirm
`docker exec cognee ls /app/sitecustomize.py` exists and that the `uv pip install`
of `cognee-community-hybrid-adapter-falkor` succeeded in the build logs
(`docker compose build cognee` and read the output). Fix and rebuild. Do not
proceed until this prints `True`.

## Step 4 — Point the OpenClaw plugin at this Cognee

The `cognee-openclaw` plugin is backend-agnostic — it only needs the API URL. In
`~/.openclaw/openclaw.json`:

```json5
{
  plugins: {
    slots: { memory: "cognee-openclaw" },
    entries: {
      "cognee-openclaw": {
        enabled: true,
        hooks: { allowConversationAccess: true },
        config: {
          baseUrl: "http://localhost:8000",
          // For multiple agents: give each its own graph.
          agentDatasetPrefix: "openclaw-cognee-agent",
          recallScopes: ["agent"],
          defaultWriteScope: "agent"
        }
      }
    }
  }
}
```

**Per-agent graphs:** when the gateway hosts more than one agent
(`agents.list.length > 1`), the plugin auto-enables per-agent memory — each
agent's files + chat route to `{agentDatasetPrefix}-{agentId}` (agentId
lowercased), i.e. its own FalkorDB graph. Give each agent its own `workspace` in
`agents.list` and seed a one-line `MEMORY.md` in each so its dataset is created
on gateway start. Force the mode with `perAgentMemory: true|false` if needed.

## Step 5 — Verify end-to-end

```bash
openclaw gateway stop && openclaw gateway start

# Datasets show up in Cognee as agents become active:
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode username=default_user@example.com \
  --data-urlencode password=default_password \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s http://localhost:8000/api/v1/datasets -H "Authorization: Bearer $TOKEN"

# One FalkorDB graph per dataset_id:
docker exec falkordb redis-cli GRAPH.LIST
```

Chat-derived memory only lands in the graph after the session-cache → `/improve`
bridge runs (on `session_end`, or force it with
`openclaw cognee improve --dataset <name>`). For a quick multi-agent isolation
check: tell agent A something, end its session, ask agent B — B should not know it.

## Operations & cleanup

```bash
docker compose logs -f cognee      # follow Cognee logs
docker compose restart cognee      # after env changes
docker compose down                # stop, keep data
docker compose down -v             # stop AND wipe Cognee + FalkorDB data
```

Wipe Cognee data via API (keeps containers), then clear the plugin's local state:
```bash
openclaw cognee forget --everything --confirm
rm -f ~/.openclaw/memory/cognee/*.json
```

## Gotchas (each has bitten a real setup)

- **Stock image won't work via env vars alone.** Without `sitecustomize.py` importing `register`, `*_PROVIDER=falkor` fails at engine creation. The Step 3 check catches this.
- **Adapter version drift.** Keep the Dockerfile pin; bump it deliberately if PyPI breaks against the cognee base tag.
- **Base-image tag.** Bump `cognee/cognee:1.1.0` and the adapter pin together if you move Cognee versions.
- **`GRAPH_DATABASE_URL` must resolve to the FalkorDB host** — under this compose it's the service name `falkordb`.
- **Stale plugin state.** If datasets look empty or chat lands in the wrong graph after recreating the server, `rm -f ~/.openclaw/memory/cognee/*.json` and restart the gateway so it rebuilds against the fresh server.
- **Stable credentials.** If you set `apiKey`/`username` on the plugin, keep them constant — dataset ownership is per-user; switching identities orphans existing datasets.
