from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage


class GetDatasetsTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = self.runtime.credentials["base_url"].rstrip("/")
        api_key = self.runtime.credentials["api_key"]

        try:
            response = httpx.get(
                f"{base_url}/datasets/",
                headers={
                    "X-Api-Key": api_key,
                    "Content-Type": "application/json",
                },
                timeout=60,
            )
            response.raise_for_status()
            result = response.json()

            yield self.create_json_message(result)

            datasets = result if isinstance(result, list) else []
            if datasets:
                text_parts = [f"Found {len(datasets)} dataset(s):\n"]
                for i, ds in enumerate(datasets, 1):
                    if isinstance(ds, dict):
                        name = ds.get("name", "unknown")
                        ds_id = ds.get("id", "")
                        created = ds.get("createdAt", "")
                        text_parts.append(f"{i}. {name} (id: {ds_id}, created: {created})")
                    else:
                        text_parts.append(f"{i}. {ds}")
                formatted = "\n".join(text_parts)
            else:
                formatted = "No datasets found."

            yield self.create_variable_message("datasets_count", len(datasets))
            yield self.create_variable_message("datasets_text", formatted)
            yield self.create_text_message(formatted)
        except httpx.HTTPStatusError as e:
            error_msg = f"Cognee API error {e.response.status_code}: {e.response.text}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
        except Exception as e:
            error_msg = f"Failed to get datasets: {str(e)}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
