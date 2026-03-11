from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage


class AddDataTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = self.runtime.credentials["base_url"].rstrip("/")
        api_key = self.runtime.credentials["api_key"]

        dataset_name = tool_parameters.get("dataset_name", "")
        dataset_id = tool_parameters.get("dataset_id", "")
        text_data = tool_parameters["text_data"]
        node_set = tool_parameters.get("node_set", "")

        if not dataset_name and not dataset_id:
            error_msg = "Either dataset_name or dataset_id must be provided"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
            return

        text_items = [item.strip() for item in text_data.split("\n") if item.strip()]
        if not text_items:
            text_items = [text_data]

        body: dict[str, Any] = {"textData": text_items}
        if dataset_name:
            body["datasetName"] = dataset_name
        if dataset_id:
            body["datasetId"] = dataset_id
        if node_set:
            body["nodeSet"] = [n.strip() for n in node_set.split(",") if n.strip()]

        try:
            response = httpx.post(
                f"{base_url}/add_text",
                json=body,
                headers={
                    "X-Api-Key": api_key,
                    "Content-Type": "application/json",
                },
                timeout=60,
            )
            response.raise_for_status()
            result = response.json()

            dataset_id = ""
            data_ids: list[str] = []

            items = result if isinstance(result, list) else [result]
            for item in items:
                if isinstance(item, dict):
                    if not dataset_id and item.get("dataset_id"):
                        dataset_id = str(item["dataset_id"])
                    for info in item.get("data_ingestion_info", []):
                        if isinstance(info, dict) and info.get("data_id"):
                            data_ids.append(str(info["data_id"]))

            yield self.create_json_message(result)
            first_data_id = data_ids[0] if data_ids else ""

            yield self.create_variable_message("dataset_name", dataset_name)
            yield self.create_variable_message("dataset_id", dataset_id)
            yield self.create_variable_message("data_id", first_data_id)
            yield self.create_variable_message("items_count", len(text_items))
            yield self.create_text_message(
                f"Successfully added {len(text_items)} text item(s) to dataset '{dataset_name}'."
            )
        except httpx.HTTPStatusError as e:
            error_msg = f"Cognee API error {e.response.status_code}: {e.response.text}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
        except Exception as e:
            error_msg = f"Failed to add data: {str(e)}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
