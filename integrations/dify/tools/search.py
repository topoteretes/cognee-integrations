from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from tools.cognee_client import as_bool, authorize, base_url_of, error_text, make_client, split_csv

SEARCH_TIMEOUT = 300


def _item_text(item: Any) -> str:
    """Readable text for one entry of a ``POST /api/v1/search`` response.

    With access control on (the server default) each entry is a per-dataset
    dict ``{"dataset_id", "dataset_name", "search_result"}``; ``search_result``
    is the completion string for completion search types, or a list for
    retrieval-only types. With ``verbose`` the completion is under
    ``text_result`` and the retrieval context under ``context_result``. With
    access control off the entries are the bare results. Older shapes carried
    ``content``/``text`` keys.
    """
    if isinstance(item, dict):
        if "search_result" in item or "text_result" in item:
            inner = item.get("search_result")
            if inner is None:
                inner = item.get("text_result") or item.get("context_result") or ""
            if isinstance(inner, list):
                inner = "\n".join(_item_text(sub) for sub in inner)
            elif not isinstance(inner, str):
                inner = str(inner)
            if item.get("error"):
                inner = f"{inner}\n(error: {item['error']})" if inner else f"error: {item['error']}"
            name = item.get("dataset_name")
            return f"[{name}] {inner}" if name else inner
        for key in ("content", "text"):
            if isinstance(item.get(key), str):
                return item[key]
        return str(item)
    return str(item)


def format_results(result: Any) -> tuple[int, str]:
    """Return ``(results_count, results_text)`` for a search response body."""
    if isinstance(result, list):
        lines = [f"Found {len(result)} result(s):\n"]
        for i, item in enumerate(result, 1):
            lines.append(f"{i}. {_item_text(item)}")
        return len(result), "\n".join(lines)
    return 1, str(result)


def build_body(tool_parameters: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "searchType": tool_parameters.get("search_type", "GRAPH_COMPLETION"),
        "query": tool_parameters["query"],
        "topK": int(tool_parameters.get("top_k") or 10),
        "onlyContext": as_bool(tool_parameters.get("only_context", "false")),
        "verbose": as_bool(tool_parameters.get("verbose", "false")),
    }
    datasets = split_csv(tool_parameters.get("datasets", ""))
    dataset_ids = split_csv(tool_parameters.get("dataset_ids", ""))
    node_names = split_csv(tool_parameters.get("node_name", ""))
    if datasets:
        body["datasets"] = datasets
    if dataset_ids:
        body["datasetIds"] = dataset_ids
    if node_names:
        body["nodeName"] = node_names
    system_prompt = tool_parameters.get("system_prompt", "")
    if system_prompt:
        body["systemPrompt"] = system_prompt
    return body


class SearchTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)
        body = build_body(tool_parameters)

        try:
            with make_client() as client:
                response = client.post(
                    f"{base_url}/api/v1/search",
                    json=body,
                    headers=authorize(client, self.runtime, json=True),
                    timeout=SEARCH_TIMEOUT,
                )
                response.raise_for_status()
                result = response.json()

                yield self.create_json_message(
                    result if isinstance(result, dict) else {"results": result}
                )

                results_count, formatted = format_results(result)
                yield self.create_variable_message("results_count", results_count)
                yield self.create_variable_message("results_text", formatted)
                yield self.create_text_message(formatted)
        except Exception as e:
            error_msg = error_text(e, "Search failed")
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
