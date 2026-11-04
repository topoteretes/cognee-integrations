from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from tools.cognee_client import (
    as_bool,
    authorize,
    base_url_of,
    error_text,
    make_client,
    parse_json,
)

FORGET_TIMEOUT = 600


def build_body(tool_parameters: dict[str, Any]) -> dict[str, Any]:
    """JSON body for ``POST /api/v1/forget``.

    ``everything`` is only honoured when set explicitly to ``"true"`` in the
    tool's form section; the LLM cannot set it.
    """
    body: dict[str, Any] = {
        "memoryOnly": as_bool(tool_parameters.get("memory_only", "false")),
        "everything": as_bool(tool_parameters.get("everything", "false")),
    }
    dataset_name = tool_parameters.get("dataset_name", "")
    dataset_id = tool_parameters.get("dataset_id", "")
    data_id = tool_parameters.get("data_id", "")
    # The server rejects both dataset and datasetId; the ID wins when both are set.
    if dataset_id:
        body["datasetId"] = dataset_id
    elif dataset_name:
        body["dataset"] = dataset_name
    if data_id:
        body["dataId"] = data_id
    return body


def describe(body: dict[str, Any]) -> str:
    target = body.get("datasetId") or body.get("dataset") or ""
    if body["everything"]:
        return "all datasets and data owned by this user"
    if body.get("dataId"):
        what = f"data item '{body['dataId']}' in dataset '{target}'"
    else:
        what = f"dataset '{target}'"
    return f"the memory of {what}" if body["memoryOnly"] else what


class ForgetTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)
        body = build_body(tool_parameters)

        if not body["everything"] and not (body.get("dataset") or body.get("datasetId")):
            error_msg = "Either dataset_name or dataset_id must be provided"
            yield self.create_json_message({"succeeded": False, "error": error_msg})
            yield self.create_variable_message("succeeded", False)
            yield self.create_text_message(error_msg)
            return

        try:
            with make_client() as client:
                response = client.post(
                    f"{base_url}/api/v1/forget",
                    json=body,
                    headers=authorize(client, self.runtime, json=True),
                    timeout=FORGET_TIMEOUT,
                )
                response.raise_for_status()
                result = parse_json(response)

                yield self.create_json_message(
                    result if isinstance(result, dict) else {"result": result}
                )
                yield self.create_variable_message("succeeded", True)
                yield self.create_text_message(f"Forgot {describe(body)}.")
        except Exception as e:
            error_msg = error_text(e, "Failed to forget")
            yield self.create_json_message({"succeeded": False, "error": error_msg})
            yield self.create_variable_message("succeeded", False)
            yield self.create_text_message(error_msg)
