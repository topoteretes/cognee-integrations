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
from tools.ingestion import (
    as_json_message,
    dataset_form_fields,
    first_data_id,
    pipeline_dataset,
    text_upload,
)

ADD_TIMEOUT = 3600


class AddDataTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)

        dataset_name = tool_parameters.get("dataset_name", "")
        dataset_id = tool_parameters.get("dataset_id", "")
        text_data = tool_parameters["text_data"]

        if not dataset_name and not dataset_id:
            error_msg = "Either dataset_name or dataset_id must be provided"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
            return

        form_data = dataset_form_fields(
            dataset_name, dataset_id, split_csv(tool_parameters.get("node_set", ""))
        )

        try:
            with make_client() as client:
                response = client.post(
                    f"{base_url}/api/v1/add",
                    headers=authorize(client, self.runtime),
                    files={"data": text_upload(text_data)},
                    data=form_data,
                    timeout=ADD_TIMEOUT,
                )
                response.raise_for_status()
                result = parse_json(response)

                resp_dataset_name, resp_dataset_id = pipeline_dataset(
                    result, dataset_name, dataset_id
                )
                data_id, items_count = first_data_id(result)

                yield self.create_json_message(as_json_message(result))
                yield self.create_variable_message("dataset_name", resp_dataset_name)
                yield self.create_variable_message("dataset_id", resp_dataset_id)
                yield self.create_variable_message("data_id", data_id)
                yield self.create_variable_message("items_count", items_count)
                yield self.create_text_message(
                    f"Successfully added data to dataset '{resp_dataset_name}' "
                    f"(id: {resp_dataset_id})."
                )
        except Exception as e:
            error_msg = error_text(e, "Failed to add data")
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
