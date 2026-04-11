from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from dify_plugin.file.file import File


class AddFileTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = self.runtime.credentials["base_url"].rstrip("/")
        api_key = self.runtime.credentials["api_key"]

        file_objects: list[File] = tool_parameters.get("files", [])
        dataset_name = tool_parameters.get("dataset_name", "")
        dataset_id = tool_parameters.get("dataset_id", "")
        node_set = tool_parameters.get("node_set", "")

        if not dataset_name and not dataset_id:
            error_msg = "Either dataset_name or dataset_id must be provided"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
            return

        if not file_objects:
            error_msg = "At least one file must be provided"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
            return

        upload_files = []
        for f in file_objects:
            filename = f.filename or "file"
            mime_type = f.mime_type or "application/octet-stream"
            upload_files.append(("data", (filename, f.blob, mime_type)))

        data: dict[str, Any] = {}
        if dataset_name:
            data["datasetName"] = dataset_name
        if dataset_id:
            data["datasetId"] = dataset_id
        if node_set:
            data["node_set"] = [n.strip() for n in node_set.split(",") if n.strip()]

        try:
            response = httpx.post(
                f"{base_url}/add",
                files=upload_files,
                data=data,
                headers={"X-Api-Key": api_key},
                timeout=120,
            )
            response.raise_for_status()
            result = response.json()

            dataset_id_out = ""
            items = result if isinstance(result, list) else [result]
            for item in items:
                if isinstance(item, dict) and not dataset_id_out and item.get("dataset_id"):
                    dataset_id_out = str(item["dataset_id"])

            yield self.create_json_message(result)
            yield self.create_variable_message("dataset_name", dataset_name)
            yield self.create_variable_message("dataset_id", dataset_id_out or dataset_id)
            yield self.create_variable_message("file_count", len(file_objects))
            yield self.create_text_message(
                f"Successfully uploaded {len(file_objects)} file(s) to dataset '{dataset_name}'."
            )
        except httpx.HTTPStatusError as e:
            error_msg = f"Cognee API error {e.response.status_code}: {e.response.text}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
        except Exception as e:
            error_msg = f"Failed to upload files: {str(e)}"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
