# Dify Integration Examples

## Prerequisites

- A running Dify workspace (self-hosted or cloud).
- A [Cognee Cloud](https://platform.cognee.ai/) account.
- Your Cognee **Base URL** and **API Key**.

## Example: Ingest-and-Search Workflow

This walkthrough sets up a minimal Dify workflow that ingests data into Cognee and then searches it.

### 1. Install the plugin

In your Dify workspace, go to **Plugins > Marketplace** and install the **Cognee** tool plugin. Configure it with your Base URL and API Key.

### 2. Create a workflow

Create a new Dify workflow with the following nodes:

```text
Start -> Add Data -> Cognify -> Search -> End
```

### 3. Configure each node

**Add Data** node:

```yaml
tool: cognee / add_data
parameters:
  text_data: "Cognee is an open-source knowledge engine for AI agents. It builds knowledge graphs from unstructured data and enables semantic search."
  dataset_name: "demo-dataset"
```

**Cognify** node (runs after Add Data completes):

```yaml
tool: cognee / cognify
parameters:
  datasets: "demo-dataset"
```

**Search** node:

```yaml
tool: cognee / search
parameters:
  query: "What does Cognee do?"
  datasets: "demo-dataset"
  search_type: "GRAPH_COMPLETION"
  top_k: 5
```

### 4. Run the workflow

Execute the workflow. The **Search** node should return results about Cognee's knowledge graph capabilities based on the ingested text.

### 5. Clean up (optional)

Add a **Delete Dataset** node to remove test data:

```yaml
tool: cognee / delete_dataset
parameters:
  dataset_id: "<dataset UUID from previous steps>"
```


## Async-first Notes

Calls to Cognee can be long-running. Configure request timeouts and retries in your deployment and avoid blocking user-facing request handlers while indexing/cognify operations are in flight.
