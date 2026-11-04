## Cognee

**Author:** topoteretes
**Version:** 0.1.0
**Type:** tool

### Description

Cognee is a Dify tool plugin that gives your agents and workflows AI memory. It talks to the Cognee HTTP API, so the same plugin works with **Cognee Cloud** and with a **self-hosted Cognee server**.

It exposes Cognee's memory API — **Remember**, **Recall**, **Remember Entry**, **Forget** and **Improve** — alongside the lower-level building blocks (create dataset, add data, add file, cognify, search, list datasets and data, update, delete).

**Tested with Cognee v1.6.1.** Other versions may have different API endpoints — verify compatibility before using a different version.

### Setup

Configure the provider once with:

- **Cognee Server URL** — the root URL of your Cognee Cloud tenant (for example `https://tenant-xxx.cloud.cognee.ai`) or your self-hosted server (`http://localhost:8000`). A trailing `/api`, as entered in earlier versions of this plugin, is accepted.
- **API Key** — preferred. Required for Cognee Cloud (from the [Cognee Cloud](https://platform.cognee.ai/) dashboard). On a self-hosted server you can mint one with `POST /api/v1/auth/api-keys`.
- **User Email** / **User Password** — self-hosted alternative when no API key is set. Defaults: `default_user@example.com` / `default_password`. See [Self-hosted server](#self-hosted-server) for the one thing Cognee 1.6 needs for this to work.

The plugin validates the credentials by calling the server's health endpoint and then either listing datasets with the API key or logging in with the email and password.

### Tools

The five memory tools cover most workflows. The low-level tools remain for fine-grained control.

#### Remember

Ingest text and build the memory in one call (add + cognify, optionally followed by improve). The text is stored by content hash, so no file name is involved.

**Parameters:**
- **Text Data** (required) — Text content to remember.
- **Dataset Name** (optional) — Target dataset, created if missing. Either Dataset Name or Dataset ID must be provided.
- **Dataset ID** (optional) — UUID of an existing dataset.
- **Node Set** (optional) — Comma-separated node set names for graph organization.
- **Session ID** (optional) — Session to attribute the memory to, so Recall's session scope can find it. Cannot be combined with an ontology.
- **Custom Prompt** (optional) — Custom prompt for entity extraction.
- **Run in Background** (optional, default: false) — Return as soon as the server accepted the text. By default the tool submits the build server-side and waits until the dataset's new cognify run has finished, so a following Recall sees the memory; identical text already in the dataset is recognised and returns at once.
- **Self Improvement** (optional, default: server default) — Run the improve loop after the memory is built.

**Outputs:** `status`, `dataset_id`, `dataset_name`, `pipeline_run_id`, `items_processed`

> On Cognee Cloud the build always runs asynchronously (the server defaults `run_in_background` to true), so Remember submits in the background on every deployment and waits for the dataset's new cognify run itself. Run in Background = true skips that wait.

> Remember does not return a data ID. If a workflow later needs **Delete Data**, **Update Data** or **Forget** by data ID, ingest with **Add Data** instead, or look the ID up with **Get Dataset Data**.

#### Recall

Query the memory with automatic search routing. When a Session ID is given, entries stored for that session are checked first.

**Parameters:**
- **Query** (required) — Natural language question.
- **Datasets** / **Dataset IDs** (optional) — Comma-separated dataset names or UUIDs.
- **Session ID** (optional) — Session whose remembered questions, answers and context feed the recall.
- **Scope** (optional, default: Auto) — Memory sources: `Auto`, `graph`, `session`, `session_first`, `session_context`, `all`.
- **Search Type** (optional, default: Auto) — Override the automatically chosen strategy for the graph lookup.
- **System Prompt**, **Top K** (default: 15), **Only Context**, **Include References** — As for Search.

**Outputs:** `results_count`, `results_text`, `answer`. Entries are tagged by source: graph entries carry the answer text, session entries the stored question and answer, and a `system` entry with status `memory_warming_up` means no graph exists yet for the requested datasets. `answer` is the first graph entry's text, ready to wire into an LLM node.

#### Remember Entry

Store a question and answer in a session's memory, without rebuilding the graph. Call it after the LLM node so the next Recall with the same Session ID sees the exchange.

**Parameters:**
- **Session ID** (required) — Session identifier, e.g. the conversation ID.
- **Question** (required), **Answer** (required), **Context** (optional).
- **Dataset Name** (optional, default: `main_dataset`) / **Dataset ID** (optional).

**Outputs:** `status`, `entry_id`, `session_id`

#### Forget

Remove data from memory: a whole dataset, one data item, or only the memory (graph and embeddings) while keeping the raw data.

**Parameters:**
- **Dataset Name** / **Dataset ID** — One of the two is required; the ID wins when both are set.
- **Data ID** (optional) — Forget one item instead of the whole dataset.
- **Memory Only** (optional, default: false) — Keep the raw data so the dataset can be cognified again.
- **Everything** (form only, default: false) — Permanently delete all datasets of the configured user. Not settable by the model.

**Outputs:** `succeeded`

#### Improve

Run Cognee's self-improvement loop over a dataset. With Session IDs, the sessions' remembered entries are persisted into the permanent knowledge graph; without them only the graph enrichment stages run.

**Parameters:**
- **Dataset Name** / **Dataset ID** — One of the two is required.
- **Session IDs** (optional) — Comma-separated session IDs to bridge into the graph.
- **Build Global Context Index** (optional, default: false).
- **Run in Background** (optional, default: false).

**Outputs:** `status`, `stages_completed`, `stages_total`

#### Create Dataset

Create a dataset, or return the existing one with that name.

**Parameters:** **Dataset Name** (required)

**Outputs:** `dataset_id`, `dataset_name`

#### Add Data

Add text data to a dataset without building the memory. Text is uploaded as a file to the server. Run **Cognify** afterwards.

**Parameters:**
- **Text Data** (required) — Text content to add.
- **Dataset Name** / **Dataset ID** — One of the two is required.
- **Node Set** (optional) — Comma-separated node set names for graph organization.

**Outputs:** `dataset_name`, `dataset_id`, `data_id`, `items_count`

#### Add File

Upload files (documents, images, other types Cognee supports) into a dataset. Files come from a chat or workflow variable. Run **Cognify** afterwards.

**Parameters:**
- **Files** (required) — Files to upload.
- **Dataset Name** / **Dataset ID** — One of the two is required.
- **Node Set** (optional).

**Outputs:** `dataset_name`, `dataset_id`, `file_count`

#### Cognify

Build memory from one or more datasets. This may take several minutes depending on data volume.

**Parameters:**
- **Datasets** / **Dataset IDs** — Comma-separated names or UUIDs; one of the two is required.
- **Custom Prompt** (optional) — Custom prompt for entity extraction and graph generation.
- **Ontology Key** (optional) — Comma-separated ontology keys referencing previously uploaded ontology files.

**Outputs:** `datasets`

#### Search

Search the memory with an explicit search strategy.

**Parameters:**
- **Query** (required) — Natural language search query.
- **Datasets** / **Dataset IDs** (optional) — Comma-separated names or UUIDs.
- **Node Name** (optional) — Comma-separated node set names to restrict the search to.
- **Search Type** (required, default: `GRAPH_COMPLETION`) — `GRAPH_COMPLETION`, `HYBRID_COMPLETION`, `GRAPH_COMPLETION_DECOMPOSITION`, `GRAPH_COMPLETION_COT`, `GRAPH_COMPLETION_CONTEXT_EXTENSION`, `GRAPH_SUMMARY_COMPLETION`, `GRAPH_REPORT`, `RAG_COMPLETION`, `TRIPLET_COMPLETION`, `SUMMARIES`, `CHUNKS`, `CHUNKS_LEXICAL`, `CYPHER`, `NATURAL_LANGUAGE`, `TEMPORAL`, `FEELING_LUCKY`, `CODING_RULES`
- **System Prompt** (optional) — System prompt for Completion-type searches.
- **Top K** (optional, default: 10) — Maximum number of results to return.
- **Only Context** (optional, default: false) — Skip the LLM and return what it would have received. For completion search types this is the full user prompt (question plus retrieved context); retrieval-only types return their context.
- **Verbose** (optional, default: false) — Return the completion, retrieval context and graph objects per dataset.

**Outputs:** `results_count`, `results_text`. `results_text` lists one entry per searched dataset, prefixed with the dataset name; the raw JSON response is also emitted.

#### Get Datasets

List the datasets the configured user can read.

**Outputs:** `datasets_count`, `datasets_text`

#### Get Dataset Data

List the data items in a dataset, with their IDs.

**Parameters:** **Dataset ID** (required)

**Outputs:** `data_count`, `data_text`

#### Update Data

Replace the content of an existing data item and re-integrate the change into the memory.

**Parameters:**
- **Dataset ID** (required), **Data ID** (required), **Text Data** (required).
- **Node Set** (optional).

**Outputs:** `succeeded`, `dataset_id`, `data_id`

#### Delete Dataset

Delete an entire dataset and all its data permanently. **Forget** does the same by dataset name.

**Parameters:** **Dataset ID** (required)

**Outputs:** `succeeded`, `dataset_id`

#### Delete Data

Delete a specific data item from a dataset.

**Parameters:** **Dataset ID** (required), **Data ID** (required)

**Outputs:** `succeeded`, `dataset_id`, `data_id`

### Usage in Dify Workflows

The two-node happy path:

1. Use **Remember** to store text and build the memory in one step.
2. Use **Recall** before the LLM node and wire its `answer` (or `results_text`) into the prompt.

For chat memory across runs, pass the conversation ID as **Session ID** to Recall, and add a **Remember Entry** node after the LLM node with the same Session ID. Run **Improve** with those session IDs periodically to persist them into the graph.

Fine-grained control:

- **Create Dataset**, **Add Data** or **Add File**, then **Cognify** instead of Remember when you need the data ID, upload files, or run cognify separately.
- **Search** instead of Recall to pick the search type yourself.
- **Get Datasets** and **Get Dataset Data** to look up IDs.
- **Update Data** to modify an existing data item.
- **Forget** (preferred), or **Delete Dataset** / **Delete Data**, to remove data.

---

### Cognee Cloud

1. Get your API key and tenant URL from the [Cognee Cloud](https://platform.cognee.ai/) dashboard.
2. Install the plugin in your Dify workspace.
3. Configure it with the **Cognee Server URL** and **API Key**. Leave the email and password empty.

### Self-hosted server

A running Cognee v1.6.1 server reachable from the Dify plugin. There are two ways to run it:

> **Cognee 1.6 and the default user:** since 1.6 the server only gives the default user a password when it is started with `DEFAULT_USER_PASSWORD` set. Without it, email and password login fails with *"This user has no password"*; use an API key instead, or set the variable. Both options below set it. Existing installs keep whatever password the default user already has; the server never rewrites it.

#### Option A: Docker (recommended)

The ready-made file lives in [`docker/docker-compose.yml`](docker/docker-compose.yml):

```yaml
# docker-compose.yml
services:
  cognee:
    image: cognee/cognee:1.6.1
    container_name: cognee-local
    ports:
      - "8000:8000"
    environment:
      - HOST=0.0.0.0
      - ENVIRONMENT=local
      # Must match the User Email / User Password configured in the plugin.
      - DEFAULT_USER_EMAIL=default_user@example.com
      - DEFAULT_USER_PASSWORD=default_password
    volumes:
      - .env:/app/.env
      # Persist databases and raw files across restarts.
      - cognee_system:/cognee-storage/system
      - cognee_data:/cognee-storage/data
volumes:
  cognee_system:
  cognee_data:
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
pip install cognee==1.6.1
```

Start the Cognee API server (set the LLM key and the default-user password for this shell session only):

```bash
LLM_API_KEY=sk-your-openai-key-here \
DEFAULT_USER_EMAIL=default_user@example.com \
DEFAULT_USER_PASSWORD=default_password \
python -m cognee.api.client
```

The server starts on `http://localhost:8000`. Verify with `curl http://localhost:8000/health`.

> **Note:** The pip method requires you to manage your own Python environment and dependencies. Docker is simpler for most users.

Default credentials for both methods: `default_user@example.com` / `default_password`. To use a different account, set `DEFAULT_USER_EMAIL` / `DEFAULT_USER_PASSWORD` on the server to the values you configure in the plugin.

#### Using an API key with a self-hosted server

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -d 'username=default_user@example.com&password=default_password' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s -X POST http://localhost:8000/api/v1/auth/api-keys -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"name":"dify"}'
```

Put the returned `key` into the plugin's **API Key** field and leave the email and password empty.

---

### Testing Guide

Follow these steps to test the plugin end-to-end with Cognee and Dify running in Docker.

#### Step 1: Start Cognee

See [Self-hosted server](#self-hosted-server) above. Verify the server is running:

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
2. In the plugin directory (`integrations/dify`), create `.env`:
   ```
   INSTALL_METHOD=remote
   REMOTE_INSTALL_URL=localhost:5003
   REMOTE_INSTALL_KEY=your-debugging-key
   ```
3. Install dependencies and run:
   ```bash
   cd integrations/dify
   uv run python -m main
   ```
4. The plugin appears in Dify with a "debugging" badge. Changes take effect on restart.

**Method B: Package install**

1. Package the plugin:
   ```bash
   cd integrations
   dify plugin package ./dify
   ```
2. Go to **http://localhost/plugins** → **Install Plugin** → **Install from Local File**.
3. Upload `dify.difypkg`.

> **Note:** The Dify CLI can be installed via `brew tap langgenius/dify && brew install dify`.

#### Step 4: Configure the provider

In the Dify plugins page, find **Cognee** and click configure:

- **Cognee Server URL:** `http://localhost:8000`
- **API Key:** leave empty, or paste a key minted as shown above
- **User Email:** `default_user@example.com`
- **User Password:** `default_password`

> **Important:** Since the plugin runs on your host machine (not inside Docker), use `localhost`. If you were running the plugin inside Docker too, you'd use `host.docker.internal`.

Click **Save**. The plugin validates by performing a health check and then checking the API key or logging in.

> **"This Cognee user has no password":** the server was started without `DEFAULT_USER_PASSWORD` (see [Self-hosted server](#self-hosted-server)). Either configure an API key, or restart the server with `DEFAULT_USER_EMAIL` / `DEFAULT_USER_PASSWORD` set to the values entered here.

> **Long-running operations:** Remember, Cognify, Improve and Update can take long on large datasets. This plugin sets generous timeouts, but Dify itself has its own limits (`PLUGIN_DAEMON_TIMEOUT`, `GUNICORN_TIMEOUT`, etc. in Dify's `docker/.env`). Increase those if operations time out.

#### Step 5: Test the tools

Create a Dify workflow or use Agent mode to test. A good sequence: Remember → Recall → Remember Entry → Recall with the same Session ID → Improve → Forget.

### Migrating from the self-hosted plugin (`cognee_sdk`)

Earlier versions of this repository shipped a second, unpublished plugin named **Cognee (Self-Hosted)** with the provider id `topoteretes/cognee_sdk`. This plugin replaces it: it supports the same email and password login, the same tools with the same parameters and output variables, and adds the API-key method and the cloud-only tools.

Dify has no provider rename migration, so an installed `cognee_sdk` keeps working untouched but receives no updates. To move:

1. Install **Cognee** alongside it and configure the same server URL and credentials (or an API key).
2. In each workflow, replace every `cognee_sdk` tool node with the same tool from **Cognee**. Parameter names and output variables are unchanged, so the surrounding wiring stays valid once the node is re-pointed. Agent apps: swap the tool in the agent's tool list.
3. Uninstall `cognee_sdk`.

### Links

- [Cognee Website](https://www.cognee.ai)
- [Cognee Documentation](https://docs.cognee.ai)
- [GitHub](https://github.com/topoteretes/cognee)
