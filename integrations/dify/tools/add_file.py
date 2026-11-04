from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from dify_plugin.file.file import File
from tools.cognee_client import (
    authorize,
    base_url_of,
    error_text,
    make_client,
    parse_json,
    split_csv,
)
from tools.ingestion import as_json_message, dataset_form_fields, pipeline_dataset

ADD_TIMEOUT = 3600


class AddFileTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)

        file_objects: list[File] = tool_parameters.get("files") or []
        dataset_name = tool_parameters.get("dataset_name", "")
        dataset_id = tool_parameters.get("dataset_id", "")

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

        # Files keep their own names: the server deduplicates by name and content.
        upload_files = [
            ("data", (f.filename or "file", f.blob, f.mime_type or "application/octet-stream"))
            for f in file_objects
        ]
        form_data = dataset_form_fields(
            dataset_name, dataset_id, split_csv(tool_parameters.get("node_set", ""))
        )

        try:
            with make_client() as client:
                response = client.post(
                    f"{base_url}/api/v1/add",
                    files=upload_files,
                    data=form_data,
                    headers=authorize(client, self.runtime),
                    timeout=ADD_TIMEOUT,
                )
                response.raise_for_status()
                result = parse_json(response)

                resp_dataset_name, resp_dataset_id = pipeline_dataset(
                    result, dataset_name, dataset_id
                )

                yield self.create_json_message(as_json_message(result))
                yield self.create_variable_message("dataset_name", resp_dataset_name)
                yield self.create_variable_message("dataset_id", resp_dataset_id)
                yield self.create_variable_message("file_count", len(file_objects))
                yield self.create_text_message(
                    f"Successfully uploaded {len(file_objects)} file(s) to dataset "
                    f"'{resp_dataset_name}' (id: {resp_dataset_id})."
                )
        except Exception as e:
            error_msg = error_text(e, "Failed to upload files")
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
