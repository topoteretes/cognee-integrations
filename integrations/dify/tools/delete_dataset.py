from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage


class DeleteDatasetTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = self.runtime.credentials["base_url"].rstrip("/")
        api_key = self.runtime.credentials["api_key"]

        dataset_id = tool_parameters["dataset_id"]

        try:
            response = httpx.delete(
                f"{base_url}/datasets/{dataset_id}",
                headers={"X-Api-Key": api_key},
                timeout=30,
            )
            response.raise_for_status()

            yield self.create_json_message({"succeeded": True, "dataset_id": dataset_id})
            yield self.create_variable_message("succeeded", True)
            yield self.create_variable_message("dataset_id", dataset_id)
            yield self.create_text_message(f"Successfully deleted dataset '{dataset_id}'.")
        except Exception as e:
            yield self.create_json_message({"succeeded": False, "dataset_id": dataset_id})
            yield self.create_variable_message("succeeded", False)
            yield self.create_variable_message("dataset_id", dataset_id)
            yield self.create_text_message(f"Failed to delete dataset: {str(e)}")
