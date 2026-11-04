from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage
from tools.cognee_client import authorize, base_url_of, error_text, make_client, parse_json

REMEMBER_ENTRY_TIMEOUT = 120


def build_body(tool_parameters: dict[str, Any]) -> dict[str, Any]:
    """JSON body for ``POST /api/v1/remember/entry``.

    Unlike the other endpoints this body is snake_case only.
    """
    entry: dict[str, Any] = {
        "type": "qa",
        "question": tool_parameters["question"],
        "answer": tool_parameters["answer"],
    }
    context = tool_parameters.get("context", "")
    if context:
        entry["context"] = context

    body: dict[str, Any] = {
        "entry": entry,
        "session_id": tool_parameters["session_id"],
    }
    dataset_id = tool_parameters.get("dataset_id", "")
    dataset_name = tool_parameters.get("dataset_name", "")
    if dataset_id:
        body["dataset_id"] = dataset_id
    if dataset_name:
        body["dataset_name"] = dataset_name
    return body


class RememberEntryTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)
        body = build_body(tool_parameters)

        try:
            with make_client() as client:
                response = client.post(
                    f"{base_url}/api/v1/remember/entry",
                    json=body,
                    headers=authorize(client, self.runtime, json=True),
                    timeout=REMEMBER_ENTRY_TIMEOUT,
                )
                response.raise_for_status()
                result = parse_json(response)

                status = str(result.get("status", "session_stored"))
                entry_id = str(result.get("entry_id") or "")

                yield self.create_json_message(result)
                yield self.create_variable_message("status", status)
                yield self.create_variable_message("entry_id", entry_id)
                yield self.create_variable_message("session_id", body["session_id"])
                yield self.create_text_message(
                    f"Stored the question and answer in session '{body['session_id']}'"
                    + (f" (entry id: {entry_id})." if entry_id else ".")
                )
        except Exception as e:
            error_msg = error_text(e, "Failed to remember the entry")
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
