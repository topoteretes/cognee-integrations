# cognee memory for Vellum Workflows

`cognee-integration-vellum` gives any [Vellum](https://www.vellum.ai/) workflow
persistent, structured memory backed by cognee's knowledge graph. Vellum
workflows are stateless between executions — cognee makes them remember.

It ships two things, both built on cognee's public `remember()` / `recall()` API
(no reimplemented ingestion or session handling):

- **Workflow nodes** (`CogneeRememberNode`, `CogneeRecallNode`) — Vellum
  Workflows SDK `BaseNode` subclasses. Once pushed, they render as first-class
  drag-and-drop blocks in the visual editor.
- **Agent Node tools** (`cognee_remember`, `cognee_recall`) — thin functions to
  register as custom tools so a Vellum agent can decide *when* to read or write
  memory.

## Run your own in 5 minutes

1. **Install**
   ```bash
   pip install cognee-integration-vellum
   ```
2. **Set two env vars** (cognee endpoint + API key — never hardcode them in a
   node; use Vellum workspace secrets in the editor):
   ```bash
   export COGNEE_BASE_URL="http://localhost:8000"   # or your Cognee Cloud URL
   export COGNEE_API_KEY="your_cognee_api_key"
   ```
3. **Push the example workflow** so the cognee nodes appear in the editor:
   ```bash
   vellum push examples/support_assistant_workflow.py
   ```
4. Open the workflow in Vellum — `CogneeRememberNode` and `CogneeRecallNode` are
   now reusable blocks you can drag into any workflow.

## Nodes

### `CogneeRememberNode`
Stores data in cognee memory. **Synchronous by default** — it blocks until
cognee finishes building the graph, so `status` / `error` are real the moment the
node completes and downstream nodes can branch on them.

| Input | Default | Notes |
|---|---|---|
| `data` | `""` | Text to store |
| `dataset_name` | `main_dataset` | One workflow deployment ↦ one dataset |
| `user_id` | `""` | Optional per-end-user scope (maps to a cognee node set) |
| `run_in_background` | `False` | Opt-in fire-and-return for large batch ingests |

Outputs: `status`, `pipeline_run_id`, `error`, `dataset_name`.

### `CogneeRecallNode`
Answers from cognee memory with citations to the source data.

| Input | Default | Notes |
|---|---|---|
| `query` | `""` | Natural-language question |
| `dataset_name` | `main_dataset` | Dataset to search |
| `user_id` | `""` | Optional per-end-user scope |
| `top_k` | `15` | Max results |

Outputs: `answer`, `citations` (which dataset/document/chunk each hit came from),
`results` (full typed recall payload).

## Agent Node tools

`cognee_remember` and `cognee_recall` are plain typed functions — register them on
a Vellum Agent Node (`ToolCallingNode`) and the agent decides when to read/write
memory. Vellum infers each tool's schema from the function signature, so no
decorator or manual schema is needed:

```python
from vellum.workflows.nodes.displayable import ToolCallingNode
from cognee_integration_vellum import cognee_recall, cognee_remember


class MemoryAgent(ToolCallingNode):
    ml_model = "gpt-4o-mini"
    functions = [cognee_remember, cognee_recall]
```

## Memory mapping

- One Vellum **workflow deployment** ↦ one cognee **dataset** (`dataset_name`).
- **Per-end-user** scoping via a `user_id` workflow input, mapped to a cognee
  node set.
- **Credentials** come from Vellum workspace secrets / environment — never
  hardcoded in a node.

## Zero-code alternative (and when to prefer it)

Vellum's Agent Node already supports MCP tools, so
[`cognee-mcp`](https://github.com/topoteretes/cognee/tree/main/cognee-mcp) works
inside an Agent Node with **zero code**. Prefer that when you want the quickest
generic setup and don't need memory to be visible in the editor. Prefer the
**native nodes here** when you want first-class drag-and-drop blocks, typed
citation outputs, and memory that non-technical builders can see and wire up in
the visual editor.
