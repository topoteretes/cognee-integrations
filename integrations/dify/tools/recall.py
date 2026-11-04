import json
from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from tools.cognee_client import (
    as_bool,
    authorize,
    base_url_of,
    error_text,
    make_client,
    split_csv,
)

RECALL_TIMEOUT = 300
AUTO = "AUTO"


def build_body(tool_parameters: dict[str, Any]) -> dict[str, Any]:
    """JSON body for ``POST /api/v1/recall``.

    ``stream`` is pinned to false because the endpoint otherwise negotiates
    server-sent events on the Accept header.
    """
    body: dict[str, Any] = {
        "query": tool_parameters["query"],
        "topK": int(tool_parameters.get("top_k") or 15),
        "onlyContext": as_bool(tool_parameters.get("only_context", "false")),
        "includeReferences": as_bool(tool_parameters.get("include_references", "false")),
        "stream": False,
    }

    search_type = tool_parameters.get("search_type", AUTO)
    if search_type and search_type != AUTO:
        body["searchType"] = search_type

    scope = tool_parameters.get("scope", AUTO)
    if scope and scope != AUTO:
        body["scope"] = scope

    datasets = split_csv(tool_parameters.get("datasets", ""))
    dataset_ids = split_csv(tool_parameters.get("dataset_ids", ""))
    if datasets:
        body["datasets"] = datasets
    if dataset_ids:
        body["datasetIds"] = dataset_ids

    session_id = tool_parameters.get("session_id", "")
    if session_id:
        body["sessionId"] = session_id

    system_prompt = tool_parameters.get("system_prompt", "")
    if system_prompt:
        body["systemPrompt"] = system_prompt

    return body


def entry_text(item: Any) -> str:
    """Readable text for one recall entry; entries are tagged by ``source``."""
    if not isinstance(item, dict):
        return str(item)

    source = item.get("source", "")
    if source == "session":
        text = f"Q: {item.get('question', '')}\nA: {item.get('answer', '')}"
    elif source == "session_context":
        text = str(item.get("content", ""))
    elif source == "system":
        text = f"[{item.get('status', 'system')}] {item.get('text', '')}"
    elif isinstance(item.get("text"), str):
        # graph, code, tools, skills and trace entries all carry ``text``
        text = item["text"]
    elif isinstance(item.get("content"), str):
        text = item["content"]
    elif "search_result" in item:
        inner = item["search_result"]
        text = inner if isinstance(inner, str) else json.dumps(inner, default=str)
    else:
        text = json.dumps(item, default=str)

    dataset_name = item.get("dataset_name")
    if dataset_name and source in ("graph", "code", ""):
        return f"[{dataset_name}] {text}"
    return text


def format_results(result: Any) -> tuple[int, str, str]:
    """Return ``(results_count, results_text, answer)`` for a recall response body.

    ``answer`` is the first graph entry's text, falling back to the first
    entry of any kind, so a workflow can pass it straight to an LLM node.
    """
    if not isinstance(result, list):
        text = str(result)
        return 1, text, text

    if not result:
        return 0, "No results.", ""

    lines = [f"Found {len(result)} result(s):\n"]
    answer = ""
    for i, item in enumerate(result, 1):
        lines.append(f"{i}. {entry_text(item)}")
        if not answer and isinstance(item, dict) and item.get("source") == "graph":
            answer = str(item.get("text", ""))
    if not answer:
        first = result[0]
        answer = (
            str(first.get("text") or first.get("answer") or first.get("content") or "")
            if (isinstance(first, dict))
            else str(first)
        )
    return len(result), "\n".join(lines), answer


class RecallTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)
        body = build_body(tool_parameters)

        try:
            with make_client() as client:
                response = client.post(
                    f"{base_url}/api/v1/recall",
                    json=body,
                    headers=authorize(client, self.runtime, json=True),
                    timeout=RECALL_TIMEOUT,
                )
                response.raise_for_status()
                result = response.json()

                yield self.create_json_message(
                    result if isinstance(result, dict) else {"results": result}
                )

                results_count, formatted, answer = format_results(result)
                yield self.create_variable_message("results_count", results_count)
                yield self.create_variable_message("results_text", formatted)
                yield self.create_variable_message("answer", answer)
                yield self.create_text_message(formatted)
        except Exception as e:
            error_msg = error_text(e, "Recall failed")
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
