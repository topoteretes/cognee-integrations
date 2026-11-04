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

COGNIFY_TIMEOUT = 21600


class CognifyTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)

        datasets = split_csv(tool_parameters.get("datasets", ""))
        dataset_ids = split_csv(tool_parameters.get("dataset_ids", ""))
        custom_prompt = tool_parameters.get("custom_prompt", "")
        ontology_keys = split_csv(tool_parameters.get("ontology_key", ""))

        if not datasets and not dataset_ids:
            error_msg = "Either datasets or dataset_ids must be provided"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
            return

        body: dict[str, Any] = {}
        if datasets:
            body["datasets"] = datasets
        if dataset_ids:
            body["datasetIds"] = dataset_ids
        if custom_prompt:
            body["customPrompt"] = custom_prompt
        if ontology_keys:
            body["ontologyKey"] = ontology_keys

        try:
            with make_client() as client:
                response = client.post(
                    f"{base_url}/api/v1/cognify",
                    json=body,
                    headers=authorize(client, self.runtime, json=True),
                    timeout=COGNIFY_TIMEOUT,
                )
                response.raise_for_status()
                result = parse_json(response)

                label = ", ".join(datasets) if datasets else ", ".join(dataset_ids)
                yield self.create_json_message(
                    result if isinstance(result, dict) else {"result": result}
                )
                yield self.create_variable_message("datasets", label)
                yield self.create_text_message(f"Successfully cognified dataset(s): {label}")
        except Exception as e:
            error_msg = error_text(e, "Failed to cognify")
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
