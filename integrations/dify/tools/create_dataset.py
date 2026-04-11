from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage


class CreateDatasetTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = self.runtime.credentials["base_url"].rstrip("/")
        api_key = self.runtime.credentials["api_key"]

        name = tool_parameters["name"]

        try:
            response = httpx.post(
                f"{base_url}/datasets/",
                json={"name": name},
                headers={
                    "X-Api-Key": api_key,
                    "Content-Type": "application/json",
                },
                timeout=60,
            )
            response.raise_for_status()
            result = response.json()

            dataset_id = ""
            dataset_name = name
            if isinstance(result, dict):
                dataset_id = str(result.get("id", ""))
                dataset_name = result.get("name", name)

            yield self.create_json_message(result)
            yield self.create_variable_message("dataset_id", dataset_id)
            yield self.create_variable_message("dataset_name", dataset_name)
            yield self.create_text_message(
                f"Dataset '{dataset_name}' ready (id: {dataset_id})."
            )
        except httpx.HTTPStatusError as e:
            error_msg = f"Cognee API error {e.response.status_code}: {e.response.text}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
        except Exception as e:
            error_msg = f"Failed to create dataset: {str(e)}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
