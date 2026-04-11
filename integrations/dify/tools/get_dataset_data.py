from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage


class GetDatasetDataTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = self.runtime.credentials["base_url"].rstrip("/")
        api_key = self.runtime.credentials["api_key"]

        dataset_id = tool_parameters["dataset_id"]

        try:
            response = httpx.get(
                f"{base_url}/datasets/{dataset_id}/data",
                headers={
                    "X-Api-Key": api_key,
                    "Content-Type": "application/json",
                },
                timeout=60,
            )
            response.raise_for_status()
            result = response.json()

            yield self.create_json_message(result)

            items = result if isinstance(result, list) else []
            if items:
                text_parts = [f"Found {len(items)} data item(s):\n"]
                for i, item in enumerate(items, 1):
                    if isinstance(item, dict):
                        name = item.get("name", "unknown")
                        data_id = item.get("id", "")
                        ext = item.get("extension", "")
                        mime = item.get("mimeType", "")
                        created = item.get("createdAt", "")
                        text_parts.append(
                            f"{i}. {name} (id: {data_id}, type: {ext or mime}, created: {created})"
                        )
                    else:
                        text_parts.append(f"{i}. {item}")
                formatted = "\n".join(text_parts)
            else:
                formatted = "No data items found in this dataset."

            yield self.create_variable_message("data_count", len(items))
            yield self.create_variable_message("data_text", formatted)
            yield self.create_text_message(formatted)
        except httpx.HTTPStatusError as e:
            error_msg = f"Cognee API error {e.response.status_code}: {e.response.text}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
        except Exception as e:
            error_msg = f"Failed to get dataset data: {str(e)}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
