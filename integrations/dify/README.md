## Cognee

**Author:** topoteretes
**Version:** 0.0.1
**Type:** tool

### Description

Cognee is a memory plugin for Dify. It lets you ingest text data into datasets, build knowledge graphs with the Cognify engine, and search across them advanced retrieval techniques.

### Setup

1. Get your API key and base URL from your [Cognee Cloud](https://platform.cognee.ai/) dashboard.
2. Install the plugin in your Dify workspace.
3. Configure the plugin with your **Base URL** (e.g. `https://tenant-xxx.cloud.cognee.ai/api`) and **API Key**.

### Tools

#### Add Data

Add text data to a Cognee dataset. Text can contain multiple items separated by newlines.

**Parameters:**
- **Text Data** (required) - Text content to add
- **Dataset Name** (optional) - Name of the target dataset. Either Dataset Name or Dataset ID must be provided.
- **Dataset ID** (optional) - UUID of an existing dataset. Either Dataset Name or Dataset ID must be provided.
- **Node Set** (optional) - Comma-separated node set names for graph organization

#### Cognify

Build a knowledge engine from one or more datasets. This processes the ingested data and may take several minutes depending on data volume.

**Parameters:**
- **Datasets** (optional) - Comma-separated list of dataset names. Either Datasets or Dataset IDs must be provided.
- **Dataset IDs** (optional) - Comma-separated list of dataset UUIDs. Either Datasets or Dataset IDs must be provided.
- **Custom Prompt** (optional) - Custom prompt for entity extraction and graph generation
- **Ontology Key** (optional) - Comma-separated ontology keys referencing previously uploaded ontology files

#### Search

Search the Cognee memory for relevant information.

**Parameters:**
- **Query** (required) - Natural language search query
- **Datasets** (optional) - Comma-separated list of dataset names to search
- **Dataset IDs** (optional) - Comma-separated list of dataset UUIDs to search
- **Search Type** (required, default: `GRAPH_COMPLETION`) - Learn more: https://docs.cognee.ai/core-concepts/main-operations/search

- **System Prompt** (optional) - System prompt for Completion-type searches
- **Top K** (optional, default: 10) - Maximum number of results to return
- **Only Context** (optional, default: false) - Return raw context instead of LLM completion

#### Delete Dataset

Delete an entire dataset and all its data permanently.

**Parameters:**
- **Dataset ID** (required) - UUID of the dataset to delete

#### Delete Data

Delete a specific data item from a dataset.

**Parameters:**
- **Dataset ID** (required) - UUID of the dataset
- **Data ID** (required) - UUID of the data item to delete

### Usage in Dify Workflows

1. Use **Add Data** to ingest text into a dataset.
2. Use **Cognify** to build the knowledge engine from that dataset.
3. Use **Search** before LLM calls to provide relevant context from memory.
4. Use **Delete Dataset** or **Delete Data** to manage your datasets.

### Links

- [Cognee Website](https://www.cognee.ai)
- [Cognee Documentation](https://docs.cognee.ai)
- [GitHub](https://github.com/topoteretes/cognee)
