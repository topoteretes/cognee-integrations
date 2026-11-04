from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from tools.cognee_client import authorize, base_url_of, error_text, make_client

LIST_TIMEOUT = 120


def format_items(result: Any) -> tuple[int, str]:
    items = result if isinstance(result, list) else []
    if not items:
        return 0, "No data items found in this dataset."
    lines = [f"Found {len(items)} data item(s):\n"]
    for i, item in enumerate(items, 1):
        if isinstance(item, dict):
            kind = item.get("extension") or item.get("mimeType") or ""
            lines.append(
                f"{i}. {item.get('name', 'unknown')} (id: {item.get('id', '')}, type: {kind}, "
                f"created: {item.get('createdAt', '')})"
            )
        else:
            lines.append(f"{i}. {item}")
    return len(items), "\n".join(lines)


class GetDatasetDataTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)
        dataset_id = tool_parameters["dataset_id"]

        try:
            with make_client() as client:
                response = client.get(
                    f"{base_url}/api/v1/datasets/{dataset_id}/data",
                    headers=authorize(client, self.runtime),
                    timeout=LIST_TIMEOUT,
                )
                response.raise_for_status()
                result = response.json()

                count, formatted = format_items(result)
                yield self.create_json_message(
                    {"data": result} if isinstance(result, list) else result
                )
                yield self.create_variable_message("data_count", count)
                yield self.create_variable_message("data_text", formatted)
                yield self.create_text_message(formatted)
        except Exception as e:
            error_msg = error_text(e, "Failed to get dataset data")
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
