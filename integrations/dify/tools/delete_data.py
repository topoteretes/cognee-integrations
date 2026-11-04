from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from tools.cognee_client import authorize, base_url_of, error_text, make_client

DELETE_TIMEOUT = 600


class DeleteDataTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)

        dataset_id = tool_parameters["dataset_id"]
        data_id = tool_parameters["data_id"]

        try:
            with make_client() as client:
                response = client.delete(
                    f"{base_url}/api/v1/datasets/{dataset_id}/data/{data_id}",
                    headers=authorize(client, self.runtime),
                    timeout=DELETE_TIMEOUT,
                )
                response.raise_for_status()

                yield self.create_json_message(
                    {"succeeded": True, "dataset_id": dataset_id, "data_id": data_id}
                )
                yield self.create_variable_message("succeeded", True)
                yield self.create_variable_message("dataset_id", dataset_id)
                yield self.create_variable_message("data_id", data_id)
                yield self.create_text_message(
                    f"Successfully deleted data item '{data_id}' from dataset '{dataset_id}'."
                )
        except Exception as e:
            error_msg = error_text(e, "Failed to delete data")
            yield self.create_json_message(
                {
                    "succeeded": False,
                    "dataset_id": dataset_id,
                    "data_id": data_id,
                    "error": error_msg,
                }
            )
            yield self.create_variable_message("succeeded", False)
            yield self.create_variable_message("dataset_id", dataset_id)
            yield self.create_variable_message("data_id", data_id)
            yield self.create_text_message(error_msg)
