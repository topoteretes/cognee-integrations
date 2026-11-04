from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from tools.cognee_client import authorize, base_url_of, error_text, make_client

CREATE_TIMEOUT = 120


class CreateDatasetTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)
        name = tool_parameters["name"]

        try:
            with make_client() as client:
                # Returns the existing dataset when one with this name already exists.
                response = client.post(
                    f"{base_url}/api/v1/datasets",
                    json={"name": name},
                    headers=authorize(client, self.runtime, json=True),
                    timeout=CREATE_TIMEOUT,
                )
                response.raise_for_status()
                result = response.json()

                dataset_id = str(result.get("id", "")) if isinstance(result, dict) else ""
                dataset_name = result.get("name", name) if isinstance(result, dict) else name

                yield self.create_json_message(
                    result if isinstance(result, dict) else {"result": result}
                )
                yield self.create_variable_message("dataset_id", dataset_id)
                yield self.create_variable_message("dataset_name", dataset_name)
                yield self.create_text_message(
                    f"Dataset '{dataset_name}' ready (id: {dataset_id})."
                )
        except Exception as e:
            error_msg = error_text(e, "Failed to create dataset")
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
