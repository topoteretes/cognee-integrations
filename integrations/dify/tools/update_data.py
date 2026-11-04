from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from tools.cognee_client import (
    authorize,
    base_url_of,
    error_text,
    make_client,
    parse_json,
    split_csv,
)
from tools.ingestion import as_json_message, dataset_form_fields, text_upload

UPDATE_TIMEOUT = 21600


class UpdateDataTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)

        dataset_id = tool_parameters["dataset_id"]
        data_id = tool_parameters["data_id"]
        text_data = tool_parameters["text_data"]
        # The update route takes the dataset and data ids as query parameters;
        # only the node set travels in the form.
        form_data = dataset_form_fields("", "", split_csv(tool_parameters.get("node_set", "")))

        try:
            with make_client() as client:
                response = client.patch(
                    f"{base_url}/api/v1/update",
                    params={"data_id": data_id, "dataset_id": dataset_id},
                    headers=authorize(client, self.runtime),
                    files={"data": text_upload(text_data)},
                    data=form_data,
                    timeout=UPDATE_TIMEOUT,
                )
                response.raise_for_status()
                result = parse_json(response)

                status = result.get("status", "ok") if isinstance(result, dict) else "ok"
                yield self.create_json_message(as_json_message(result))
                yield self.create_variable_message("succeeded", True)
                yield self.create_variable_message("dataset_id", dataset_id)
                yield self.create_variable_message("data_id", data_id)
                yield self.create_text_message(
                    f"Successfully updated data item '{data_id}' in dataset '{dataset_id}' "
                    f"(status: {status})."
                )
        except Exception as e:
            error_msg = error_text(e, "Failed to update data")
            yield self.create_json_message({"succeeded": False, "error": error_msg})
            yield self.create_variable_message("succeeded", False)
            yield self.create_variable_message("dataset_id", dataset_id)
            yield self.create_variable_message("data_id", data_id)
            yield self.create_text_message(error_msg)
