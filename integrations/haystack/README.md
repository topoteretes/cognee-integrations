# Cognee-Integration-Haystack

A `CogneeRetriever` and `CogneeWriter` for [Haystack](https://github.com/deepset-ai/haystack), backed by [Cognee's memory layer](https://github.com/topoteretes/cognee). Haystack pipelines chain retrieval steps together well, but have no memory of their own between runs; these components give a pipeline a persistent knowledge graph to read from and write into, as real pipeline steps rather than a wrapped function call.

> **Note:** This package requires Python 3.10+.

## Overview

`cognee-integration-haystack` implements Haystack's component protocol directly:

- **`CogneeRetriever`**: a `run(query, top_k=None) -> {"documents": [...]}` retriever. Connects to any component with a `documents: List[Document]` input, the same as Haystack's built-in retrievers.
- **`CogneeWriter`**: a `run(documents) -> {"documents_written": n}` writer. Connects to any component that outputs `List[Document]` (converters, splitters, embedders), the same as `DocumentWriter`.

Both call the same `cognee.remember` / `cognee.recall` API the [strands integration](../strands) uses, so behavior is consistent across integrations.

## Installation

```bash
pip install cognee-integration-haystack
```

## Quick Start

```python
from haystack import Document, Pipeline
from cognee_integration_haystack import CogneeWriter, CogneeRetriever

# Write: push documents into cognee's knowledge graph
write_pipeline = Pipeline()
write_pipeline.add_component("writer", CogneeWriter())
write_pipeline.run(
    {
        "writer": {
            "documents": [
                Document(content="We signed a contract with Meditech Solutions for £1.2M."),
            ]
        }
    }
)

# Read: retrieve relevant documents back out, as a normal pipeline step
read_pipeline = Pipeline()
read_pipeline.add_component("retriever", CogneeRetriever(top_k=5))
result = read_pipeline.run(
    {"retriever": {"query": "What is the Meditech Solutions contract worth?"}}
)
print(result["retriever"]["documents"])
```

## Composing with the rest of a pipeline

`CogneeRetriever`'s `documents` output and `CogneeWriter`'s `documents` input are ordinary Haystack sockets, so they connect like any other component:

```python
from haystack import Pipeline
from haystack.components.builders import PromptBuilder
from cognee_integration_haystack import CogneeRetriever

pipeline = Pipeline()
pipeline.add_component("retriever", CogneeRetriever(top_k=5))
pipeline.add_component(
    "prompt",
    PromptBuilder(
        template="Answer using this context:\n{% for d in documents %}{{ d.content }}\n{% endfor %}\nQuestion: {{ query }}",
        required_variables=["documents", "query"],
    ),
)
pipeline.connect("retriever.documents", "prompt.documents")

pipeline.run(
    {
        "retriever": {"query": "What did maintainers say about PR diff hygiene?"},
        "prompt": {"query": "What did maintainers say about PR diff hygiene?"},
    }
)
```

See [`examples/example.py`](examples/example.py) for a full write-then-retrieve walkthrough, and [`examples/pipeline_example.py`](examples/pipeline_example.py) for a `CogneeRetriever` wired into a generator.

## Dataset scoping

Both components accept a dataset argument (`datasets` on the retriever, `dataset_name` on the writer), matching `cognee.recall`'s / `cognee.remember`'s own parameter names. Without one, both fall back to `COGNEE_PROJECT_NODE_SET` (the same project-scoping env var the other cognee integrations use) and then to cognee's own default (`"main_dataset"`).

```python
from cognee_integration_haystack import CogneeRetriever, CogneeWriter

writer = CogneeWriter(dataset_name="repo-review-notes")
retriever = CogneeRetriever(datasets=["repo-review-notes"])
```

> **Note:** `node_set` itself (as used by the older `add`/`cognify`/`search`-based integrations) isn't a parameter `cognee.remember`/`cognee.recall` accept; the equivalent on this API is a dataset name. Same env var, different kwarg underneath.

## Component Reference

### `CogneeRetriever(datasets=None, top_k=15, recall_kwargs=None)`

- `datasets`: Cognee dataset names to search. Defaults to `[COGNEE_PROJECT_NODE_SET]` when that env var is set, else cognee's own default.
- `top_k`: Default result count, forwarded straight to `cognee.recall`'s own `top_k`. Overridable per call via `run(query, top_k=...)`.
- `recall_kwargs`: Extra kwargs forwarded to every `cognee.recall` call (e.g. `session_id`, `query_type`).

`run(query, top_k=None) -> {"documents": List[Document]}`

### `CogneeWriter(dataset_name=None, remember_kwargs=None)`

- `dataset_name`: Cognee dataset to write into. Defaults to `COGNEE_PROJECT_NODE_SET` when set, else `"main_dataset"`.
- `remember_kwargs`: Extra kwargs forwarded to every `cognee.remember` call (e.g. `session_id`).

`run(documents: List[Document]) -> {"documents_written": int}`. Only `Document.content` is forwarded; empty-content documents are skipped and not counted.

Both components implement `to_dict`/`from_dict`, so a `Pipeline` using them serializes to and loads from YAML the normal Haystack way.

## Configuration

Copy `.env.template` to `.env` and set your key:

```bash
cp .env.template .env
```

```env
LLM_API_KEY=your-openai-api-key-here
COGNEE_PROJECT_NODE_SET=
```

## Examples

- `examples/example.py`: write a document, then retrieve it back with `CogneeRetriever`, no LLM-backed pipeline step needed.
- `examples/pipeline_example.py`: `CogneeRetriever` wired into a `PromptBuilder` + `OpenAIGenerator`, the composed-pipeline case from the [Quick Start](#composing-with-the-rest-of-a-pipeline).

## Requirements

- Python 3.10+
- Cognee `>=1.0.0,<=1.1.2`
- Haystack `haystack-ai>=2.0.0,<4.0.0`
- OpenAI API key (for the generator example)
