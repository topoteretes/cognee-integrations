## Cognee (Self-Hosted)

**Author:** topoteretes
**Version:** 0.0.1
**Type:** tool

### Description

Cognee (Self-Hosted) is a Dify tool plugin that connects to a **self-hosted Cognee server** for memory management. It lets you ingest text data into datasets, build a memory engine with the Cognify, search across them with advanced retrieval techniques, and update or delete data — all from within Dify workflows.

This plugin is designed for users running Cognee on their own infrastructure. For the cloud-hosted version, see the [Cognee (Cloud) plugin](https://github.com/topoteretes/cognee-integrations/tree/main/integrations/dify).

**Tested with Cognee v0.5.5.** Other versions may have different API endpoints — verify compatibility before using a different version.

### Tools

#### Add Data

Add text data to a Cognee dataset. Text is uploaded as a file to the server.

**Parameters:**
- **Text Data** (required) — Text content to add. Multiple items can be separated by newlines.
- **Dataset Name** (optional) — Name of the target dataset. Either Dataset Name or Dataset ID must be provided.
- **Dataset ID** (optional) — UUID of an existing dataset. Either Dataset Name or Dataset ID must be provided.
- **Node Set** (optional) — Comma-separated node set names for graph organization.

**Outputs:** `dataset_name`, `dataset_id`, `data_id`, `items_count`

#### Cognify

Build memory from one or more datasets. This processes the ingested data and may take several minutes depending on data volume.

**Parameters:**
- **Datasets** (optional) — Comma-separated list of dataset names. Either Datasets or Dataset IDs must be provided.
- **Dataset IDs** (optional) — Comma-separated list of dataset UUIDs. Either Datasets or Dataset IDs must be provided.
- **Custom Prompt** (optional) — Custom prompt for entity extraction and graph generation.
- **Ontology Key** (optional) — Comma-separated ontology keys referencing previously uploaded ontology files.

**Outputs:** `datasets`

#### Search

Search the Cognee memory for relevant information.

**Parameters:**
- **Query** (required) — Natural language search query.
- **Datasets** (optional) — Comma-separated list of dataset names to search.
- **Dataset IDs** (optional) — Comma-separated list of dataset UUIDs to search.
- **Search Type** (required, default: `GRAPH_COMPLETION`) — The search strategy. Options: `GRAPH_COMPLETION`, `GRAPH_COMPLETION_COT`, `GRAPH_COMPLETION_CONTEXT_EXTENSION`, `GRAPH_SUMMARY_COMPLETION`, `RAG_COMPLETION`, `TRIPLET_COMPLETION`, `SUMMARIES`, `CHUNKS`, `CHUNKS_LEXICAL`, `CYPHER`, `NATURAL_LANGUAGE`, `TEMPORAL`, `FEELING_LUCKY`, `CODING_RULES`
- **System Prompt** (optional) — System prompt for Completion-type searches.
- **Top K** (optional, default: 10) — Maximum number of results to return.
- **Only Context** (optional, default: false) — Return raw context instead of LLM-generated completion.

**Outputs:** `results_count`, `results_text`

#### Update Data

Update an existing data item in a dataset. Replaces the content and re-integrates changes into the memory.

**Parameters:**
- **Dataset ID** (required) — UUID of the dataset containing the data.
- **Data ID** (required) — UUID of the data item to update.
- **Text Data** (required) — New text content to replace the existing data.
- **Node Set** (optional) — Comma-separated node set names for graph organization.

**Outputs:** `succeeded`, `dataset_id`, `data_id`

#### Delete Dataset

Delete an entire dataset and all its data permanently.

**Parameters:**
- **Dataset ID** (required) — UUID of the dataset to delete.

**Outputs:** `succeeded`, `dataset_id`

#### Delete Data

Delete a specific data item from a dataset.

**Parameters:**
- **Dataset ID** (required) — UUID of the dataset.
- **Data ID** (required) — UUID of the data item to delete.

**Outputs:** `succeeded`, `dataset_id`, `data_id`

### Usage in Dify Workflows

1. Use **Add Data** to ingest text into a dataset.
2. Use **Cognify** to build the memory layer from that dataset.
3. Use **Search** before LLM calls to provide relevant context from memory.
4. Use **Update Data** to modify existing data items.
5. Use **Delete Dataset** or **Delete Data** to manage your datasets.

---

### Prerequisites

A running Cognee v0.5.5 server accessible from your Dify plugin. There are two ways to run it:

#### Option A: Docker (recommended)

```yaml
# docker-compose.yml
services:
  cognee:
    image: cognee/cognee:0.5.5
    container_name: cognee-local
    ports:
      - "8000:8000"
    environment:
      - HOST=0.0.0.0
      - ENVIRONMENT=local
    volumes:
      - .env:/app/.env
```

Create a `.env` file alongside it (**never commit this file**):

```
LLM_API_KEY=sk-your-openai-key-here
```

```bash
docker compose up -d
curl http://localhost:8000/health  # Should return HTTP 200
```

#### Option B: pip install

```bash
pip install cognee==0.5.5
```

Start the Cognee API server (set the LLM key for this shell session only):

```bash
LLM_API_KEY=sk-your-openai-key-here python -m cognee.api.client
```

The server starts on `http://localhost:8000`. Verify with `curl http://localhost:8000/health`.

> **Note:** The pip method requires you to manage your own Python environment and dependencies. Docker is simpler for most users.

Default credentials for both methods: `default_user@example.com` / `default_password`.

---

### Testing Guide

Follow these steps to test the plugin end-to-end with Cognee and Dify running in Docker.

#### Step 1: Start Cognee

See [Prerequisites](#prerequisites) above. Verify the server is running:

```bash
curl http://localhost:8000/health
```

#### Step 2: Start Dify (self-hosted)

```bash
git clone --depth 1 https://github.com/langgenius/dify.git
cd dify/docker
cp .env.example .env
```

Allow unsigned plugins (required for local development):

```bash
# In dify/docker/.env, set:
FORCE_VERIFYING_SIGNATURE=false
```

Start Dify:

```bash
docker compose up -d
```

Open **http://localhost/install** and create your admin account.

> **Note:** After the first start, the `plugin_daemon` container may fail to connect to its database (a known race condition). If you see plugin errors, run `docker compose restart plugin_daemon` and wait a few seconds.

#### Step 3: Install the plugin

There are two methods:

**Method A: Remote debug (recommended for development)**

1. Go to **http://localhost/plugins** and click the debug icon to get the debugging key.
2. In the plugin directory (integrations/dift-sdk), create `.env`:
   ```
   INSTALL_METHOD=remote
   REMOTE_INSTALL_URL=localhost:5003
   REMOTE_INSTALL_KEY=your-debugging-key
   ```
3. Install dependencies and run:
   ```bash
   cd integrations/dify-sdk
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python -m main
   ```
4. The plugin appears in Dify with a "debugging" badge. Changes take effect on restart.

**Method B: Package install**

1. Package the plugin:
   ```bash
   cd integrations
   dify plugin package ./dify-sdk
   ```
2. Go to **http://localhost/plugins** → **Install Plugin** → **Install from Local File**.
3. Upload `dify-sdk.difypkg`.

> **Note:** The Dify CLI can be installed via `brew tap langgenius/dify && brew install dify`.

#### Step 4: Configure the provider

In the Dify plugins page, find **Cognee (Self-Hosted)** and click configure:

- **Cognee Server URL:** `http://localhost:8000`
- **User Email:** `default_user@example.com`
- **User Password:** `default_password`

> **Important:** Since the plugin runs on your host machine (not inside Docker), use `localhost`. If you were running the plugin inside Docker too, you'd use `host.docker.internal`.

Click **Save**. The plugin validates by performing a health check and logging in.

> **Long-running operations:** Cognify and Update can take long on large datasets. This plugin sets generous timeouts, but Dify itself has its own limits (`PLUGIN_DAEMON_TIMEOUT`, `GUNICORN_TIMEOUT`, etc. in Dify's `docker/.env`). Increase those if operations time out.

#### Step 5: Test the tools

Create a Dify workflow or use Agent mode to test.

### Links

- [Cognee Website](https://www.cognee.ai)
- [Cognee Documentation](https://docs.cognee.ai)
- [GitHub](https://github.com/topoteretes/cognee)
