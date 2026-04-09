# OpenClaw Cognee Memory Plugin — Example Flows

## Prerequisites

- [OpenClaw](https://openclaw.dev) CLI installed (`>=2026.2.3-1`)
- Python 3.10+ and a running Cognee API server (see Step 1 below)
- An LLM API key (OpenAI, Anthropic, etc.) for Cognee's processing pipeline

---

## Flow 1: Single-Scope (Simple Project Setup)

Best for solo use or a single project where one dataset is enough.

### 1. Start Cognee

```bash
pip install "cognee[api]>=0.5.1,<0.6.0"
export LLM_API_KEY="sk-..."          # OpenAI or any supported provider
cognee-cli -api
# Cognee API now running at http://localhost:8000
```

### 2. Install and configure the plugin

```bash
openclaw plugins install @cognee/cognee-openclaw@2026.3.1

# Run the one-command setup (disables built-in memory, sets Cognee as sole provider)
openclaw cognee setup
```

This writes the following to `~/.openclaw/openclaw.json`:

```json
{
  "plugins": {
    "slots": { "memory": "cognee-openclaw" },
    "entries": {
      "memory-core": { "enabled": false },
      "memory-lancedb": { "enabled": false },
      "cognee-openclaw": { "enabled": true }
    }
  }
}
```

Then add your Cognee connection under `plugins.entries.cognee-openclaw.config`:

```json
{
  "plugins": {
    "entries": {
      "cognee-openclaw": {
        "enabled": true,
        "config": {
          "baseUrl": "http://localhost:8000",
          "apiKey": "${COGNEE_API_KEY}",
          "datasetName": "my-project"
        }
      }
    }
  }
}
```

```bash
export COGNEE_API_KEY="your-key-here"
```

### 3. Create memory files

```bash
mkdir -p memory

cat > memory/stack.md << 'EOF'
# Tech Stack

- API: REST on port 8080, JWT auth with 24h expiry
- Database: PostgreSQL 16 with pgvector for embeddings
- Frontend: Next.js 14, deployed on Vercel
- CI: GitHub Actions, deploy on merge to main
EOF

cat > MEMORY.md << 'EOF'
# Project Memory Index

Key decisions and context for the code assistant.

- prefer-postgres: always use the existing PostgreSQL DB, no SQLite
- auth-pattern: JWT only, no session cookies
EOF
```

### 4. Verify connectivity and index

```bash
# Check Cognee is reachable
openclaw cognee health
# Cognee API: OK (http://localhost:8000)

# Manually index memory files (also runs automatically on startup)
openclaw cognee index
# Sync complete: 2 added, 0 updated, 0 deleted, 0 unchanged, 0 errors

# Check what was indexed
openclaw cognee status
# Dataset: my-project
# Dataset ID: a1b2c3d4-...
# Indexed files: 2 (2 with data ID)
# Workspace files: 2
# New (unindexed): 0
# Changed (dirty): 0
```

### 5. Run the agent — what happens automatically

```bash
openclaw start
```

**On startup** (because `autoIndex: true`):
1. Health check against `http://localhost:8000/health`
2. Scan for `MEMORY.md` and `memory/**/*.md`
3. Sync any new or changed files to the `my-project` dataset via `/api/v1/add`
4. Run `cognify` to build the knowledge graph

**Before each agent prompt** (because `autoRecall: true`):
1. Search the dataset with the user's prompt as the query
2. Filter results by `minScore` (default: 0.3), take up to `maxResults` (default: 3)
3. Prepend the results to the agent's context:

```xml
<cognee_memories>
Relevant memories:
[
  {
    "id": "chunk-abc",
    "score": 0.87,
    "text": "Database: PostgreSQL 16 with pgvector for embeddings",
    "metadata": {}
  }
]
</cognee_memories>
```

**After the agent run ends** (because `autoIndex: true`):
1. Rescan memory files
2. Sync only changed or deleted files — unchanged files are skipped

---

## Flow 2: Multi-Scope (Team Setup)

Best for teams where different agents or users share company-wide knowledge but keep personal context separate.

### Workspace layout

```
my-project/
├── MEMORY.md                    # agent scope (this agent's learned behaviors)
├── memory/
│   ├── tools.md                 # agent scope (tool outputs, task context)
│   ├── company/
│   │   ├── policies.md          # company scope (shared across all users/agents)
│   │   └── domain-glossary.md   # company scope
│   └── user/
│       ├── preferences.md       # user scope (per-user preferences)
│       └── feedback.md          # user scope
```

### Configuration

```json
{
  "plugins": {
    "entries": {
      "cognee-openclaw": {
        "enabled": true,
        "config": {
          "baseUrl": "http://localhost:8000",
          "apiKey": "${COGNEE_API_KEY}",

          "companyDataset": "acme-shared",
          "userDatasetPrefix": "acme-user",
          "agentDatasetPrefix": "acme-agent",

          "userId": "${OPENCLAW_USER_ID}",
          "agentId": "code-assistant",

          "recallScopes": ["agent", "user", "company"],
          "defaultWriteScope": "agent"
        }
      }
    }
  }
}
```

```bash
export OPENCLAW_USER_ID="alice"
export COGNEE_API_KEY="your-key-here"
```

This creates three datasets:
- `acme-shared` — company scope, one dataset shared by everyone
- `acme-user-alice` — user scope, specific to Alice
- `acme-agent-code-assistant` — agent scope, specific to this agent instance

### Scope routing

Files are routed automatically based on path:

| File path | Target dataset |
|-----------|---------------|
| `memory/company/policies.md` | `acme-shared` |
| `memory/company/domain-glossary.md` | `acme-shared` |
| `memory/user/preferences.md` | `acme-user-alice` |
| `memory/user/feedback.md` | `acme-user-alice` |
| `MEMORY.md` | `acme-agent-code-assistant` |
| `memory/tools.md` | `acme-agent-code-assistant` |

Check routing for all workspace files:

```bash
openclaw cognee scopes

# [COMPANY] -> dataset "acme-shared"
#   memory/company/policies.md
#   memory/company/domain-glossary.md
#
# [USER] -> dataset "acme-user-alice"
#   memory/user/preferences.md
#   memory/user/feedback.md
#
# [AGENT] -> dataset "acme-agent-code-assistant"
#   MEMORY.md
#   memory/tools.md
```

### Multi-scope recall

Before each agent run, the plugin searches all three scopes in parallel. Results are labeled by scope so the agent can distinguish between personal preferences, shared company knowledge, and its own learned patterns:

```xml
<cognee_memories>
  <agent_memory>[
    { "id": "...", "score": 0.91, "text": "prefer-postgres: always use PostgreSQL, no SQLite" }
  ]</agent_memory>
  <user_memory>[
    { "id": "...", "score": 0.78, "text": "Alice prefers concise code reviews, no inline comments" }
  ]</user_memory>
  <company_memory>[
    { "id": "...", "score": 0.85, "text": "All services must pass SOC2 audit requirements" }
  ]</company_memory>
</cognee_memories>
```

### Multi-scope status

```bash
openclaw cognee status

# [COMPANY] Dataset: acme-shared
#   Dataset ID: f1e2d3c4-...
#   Indexed files: 2
#   Workspace files: 2
#   New (unindexed): 0
#   Changed (dirty): 0
#
# [USER] Dataset: acme-user-alice
#   Dataset ID: a9b8c7d6-...
#   Indexed files: 2
#   Workspace files: 2
#   New (unindexed): 0
#   Changed (dirty): 0
#
# [AGENT] Dataset: acme-agent-code-assistant
#   Dataset ID: 11223344-...
#   Indexed files: 2
#   Workspace files: 2
#   New (unindexed): 0
#   Changed (dirty): 0
```

---

## Flow 3: Hybrid Mode (Cognee + Built-in Memory)

If you want Cognee recall alongside OpenClaw's built-in memory search:

```bash
openclaw cognee setup --hybrid
```

This keeps `memory-core` enabled. Both providers run during recall — the agent receives context from both sources.

---

## Flow 4: Migrating from Single-Scope to Multi-Scope

If you already have a `sync-index.json` from single-scope use and want to switch to multi-scope, set any of `companyDataset`, `userDatasetPrefix`, or `agentDatasetPrefix` in config.

On the next startup, the plugin detects that scoped indexes are empty and automatically migrates your existing `sync-index.json` entries into the `defaultWriteScope` (default: `agent`). You'll see:

```
cognee-openclaw: migrated legacy sync index to scope "agent"
```

No data is lost — the old `sync-index.json` is left in place but no longer read.

---

## Flow 5: Debugging Connectivity

```bash
# Is Cognee running?
openclaw cognee health
# OK:        Cognee API: OK (http://localhost:8000)
# NOT OK:    Cognee API: UNREACHABLE (http://localhost:8000)
#            Error: fetch failed

# What files would be indexed?
openclaw cognee scopes     # multi-scope mode
openclaw cognee status     # shows indexed vs workspace file counts and dirty state

# Force a full re-index
openclaw cognee index
# Sync complete: 0 added, 2 updated, 0 deleted, 0 unchanged, 0 errors

# Check the raw state files
cat ~/.openclaw/memory/cognee/datasets.json            # dataset name -> ID mapping
cat ~/.openclaw/memory/cognee/scoped-sync-indexes.json # per-scope file hashes (multi-scope)
cat ~/.openclaw/memory/cognee/sync-index.json          # file hashes (single-scope)
```

Common issues:

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `UNREACHABLE` on health check | Cognee server not running | `cognee-cli -api` |
| `scope(s) not yet indexed: agent` in logs | Files indexed but cognify hasn't run yet | Wait or run `openclaw cognee index` |
| Recall returns nothing | `minScore` too high or dataset empty | Lower `minScore` or check `status` |
| Wrong files going to wrong scope | Custom `scopeRouting` mismatch | Run `openclaw cognee scopes` to verify |
| 401 errors | `COGNEE_API_KEY` not set or wrong | `export COGNEE_API_KEY=...` |
