## Cognee

**Author:** topoteretes
**Version:** 0.0.2
**Type:** tool

### Description

This is an AI memory plugin for Dify. Cognee is an open-source knowledge engine that lets you ingest data in any format or structure and continuously learns to provide the right context for AI agents. It combines vector search, graph databases and cognitive science approaches to make your documents both searchable by meaning and connected by relationships as they change and evolve.

### Setup

1. Get your API key and base URL from your [Cognee Cloud](https://platform.cognee.ai/) dashboard.
2. Install the plugin in your Dify workspace.
3. Configure it with your **Base URL** (e.g. `https://tenant-xxx.cloud.cognee.ai/api`) and **API Key**.

### Tools

| Tool | Purpose |
|------|---------|
| **Create Dataset** | Create a named dataset to hold your data |
| **Add Data** | Ingest text into a dataset |
| **Add File** | Upload files (PDF, DOCX, TXT, etc.) into a dataset |
| **Cognify** | Process a dataset into a searchable knowledge engine |
| **Search** | Query the knowledge engine |
| **Get Datasets** | List all available datasets |
| **Get Dataset Data** | List all data items in a dataset |
| **Delete Dataset** | Permanently delete a dataset |
| **Delete Data** | Delete a specific data item from a dataset |

---

#### Create Dataset

Create a new dataset or return an existing one with the same name. Use this before ingesting data if you want to set up the dataset explicitly.

| Parameter | Required | Description |
|-----------|----------|-------------|
| Dataset Name | yes | Name for the new dataset |

**Returns:** `dataset_id`, `dataset_name`

---

#### Add Data

Ingest text content into a dataset. Multiple text items can be separated by newlines.

| Parameter | Required | Description |
|-----------|----------|-------------|
| Text Data | yes | Text content to add (newline-separated for multiple items) |
| Dataset Name | no | Target dataset name (either this or Dataset ID required) |
| Dataset ID | no | Target dataset UUID (either this or Dataset Name required) |
| Node Set | no | Comma-separated node set names for graph organization |

**Returns:** `dataset_id`, `dataset_name`, `data_id`, `items_count`

---

#### Add File

Upload files into a dataset. Accepts documents, images, and other file types supported by Cognee. Files are passed as variables from chat input or workflow start nodes.

| Parameter | Required | Description |
|-----------|----------|-------------|
| Files | yes | Files to upload (variable from chat or workflow) |
| Dataset Name | no | Target dataset name (either this or Dataset ID required) |
| Dataset ID | no | Target dataset UUID (either this or Dataset Name required) |
| Node Set | no | Comma-separated node set names for graph organization |

**Returns:** `dataset_id`, `dataset_name`, `file_count`

---

#### Cognify

Transform ingested data into a knowledge engine. This runs a multi-step pipeline: classifying documents, extracting text chunks, identifying entities and relationships via LLM, generating summaries, and embedding everything into vector and graph stores.

This is a long-running operation — processing time depends on data volume.

| Parameter | Required | Description |
|-----------|----------|-------------|
| Datasets | no | Comma-separated dataset names (either this or Dataset IDs required) |
| Dataset IDs | no | Comma-separated dataset UUIDs (either this or Datasets required) |
| Custom Prompt | no | Custom prompt for entity extraction and graph generation |
| Ontology Key | no | Comma-separated keys referencing previously uploaded ontology files |

**Returns:** `datasets`

---

#### Search

Search within the cognee memory using one of 14 search strategies. Each strategy is suited to different use cases.

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| Query | yes | | Natural language search query |
| Search Type | yes | `GRAPH_COMPLETION` | Search strategy (see table below) |
| Datasets | no | | Comma-separated dataset names to search |
| Dataset IDs | no | | Comma-separated dataset UUIDs to search |
| System Prompt | no | | Custom system prompt for completion-type searches |
| Node Name | no | | Comma-separated node set names to filter results |
| Top K | no | 10 | Maximum number of results |
| Only Context | no | false | Return raw retrieval context instead of LLM completion |
| Verbose | no | false | Include additional details in the response |

Read more about the search types in cognee docs: [link](https://docs.cognee.ai/core-concepts/main-operations/search)

**Returns:** `results_count`, `results_text`

---

#### Get Datasets

List all datasets accessible to the authenticated user. No parameters required.

**Returns:** `datasets_count`, `datasets_text`

---

#### Get Dataset Data

List all data items stored in a specific dataset.

| Parameter | Required | Description |
|-----------|----------|-------------|
| Dataset ID | yes | UUID of the dataset to inspect |

**Returns:** `data_count`, `data_text`

---

#### Delete Dataset

Permanently delete a dataset and all its associated data.

| Parameter | Required | Description |
|-----------|----------|-------------|
| Dataset ID | yes | UUID of the dataset to delete |

**Returns:** `succeeded`, `dataset_id`

---

#### Delete Data

Delete a specific data item from a dataset.

| Parameter | Required | Description |
|-----------|----------|-------------|
| Dataset ID | yes | UUID of the dataset |
| Data ID | yes | UUID of the data item to delete |

**Returns:** `succeeded`, `dataset_id`, `data_id`

---

### Typical Workflow

1. **Create Dataset** — set up a named dataset.
2. **Add Data** or **Add File** — ingest text or documents into the dataset.
3. **Cognify** — build the knowledge engine from the ingested data.
4. **Search** — search within cognee memory 
5. **Get Datasets** / **Get Dataset Data** — inspect what's available.
6. **Delete Data** / **Delete Dataset** — clean up when needed.

### Links

- [Cognee Website](https://www.cognee.ai)
- [Cognee Documentation](https://docs.cognee.ai)
- [GitHub](https://github.com/topoteretes/cognee)
