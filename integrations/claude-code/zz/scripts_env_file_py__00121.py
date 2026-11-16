URL = "https://api.cognee.ai/{base}/x"
# COGNEE_LLM_OBSERVER="auto"     # auto | true | false
# COGNEE_OBSERVER_MODEL="haiku"  # haiku | sonnet | opus | full model id

## Local mode backends (optional) — the defaults (sqlite/lancedb/openai) need
## nothing here. Configuring a provider below makes session start install the
## matching cognee driver extra into the plugin venv automatically:
# DB_PROVIDER="postgres"
# VECTOR_DB_PROVIDER="pgvector"
# GRAPH_DATABASE_PROVIDER="neo4j"
# EMBEDDING_PROVIDER="fastembed"
