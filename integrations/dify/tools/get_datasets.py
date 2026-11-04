from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from tools.cognee_client import authorize, base_url_of, error_text, make_client

LIST_TIMEOUT = 120


def format_datasets(result: Any) -> tuple[int, str]:
    datasets = result if isinstance(result, list) else []
    if not datasets:
        return 0, "No datasets found."
    lines = [f"Found {len(datasets)} dataset(s):\n"]
    for i, ds in enumerate(datasets, 1):
        if isinstance(ds, dict):
            lines.append(
                f"{i}. {ds.get('name', 'unknown')} (id: {ds.get('id', '')}, "
                f"created: {ds.get('createdAt', '')})"
            )
        else:
            lines.append(f"{i}. {ds}")
    return len(datasets), "\n".join(lines)


class GetDatasetsTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)

        try:
            with make_client() as client:
                response = client.get(
                    f"{base_url}/api/v1/datasets",
                    headers=authorize(client, self.runtime),
                    timeout=LIST_TIMEOUT,
                )
                response.raise_for_status()
                result = response.json()

                count, formatted = format_datasets(result)
                yield self.create_json_message(
                    {"datasets": result} if isinstance(result, list) else result
                )
                yield self.create_variable_message("datasets_count", count)
                yield self.create_variable_message("datasets_text", formatted)
                yield self.create_text_message(formatted)
        except Exception as e:
            error_msg = error_text(e, "Failed to get datasets")
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
