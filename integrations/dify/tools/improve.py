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
    split_csv,
)

IMPROVE_TIMEOUT = 21600


def build_body(tool_parameters: dict[str, Any]) -> dict[str, Any]:
    """JSON body for ``POST /api/v1/improve``."""
    body: dict[str, Any] = {
        "runInBackground": as_bool(tool_parameters.get("run_in_background", "false")),
        "buildGlobalContextIndex": as_bool(
            tool_parameters.get("build_global_context_index", "false")
        ),
    }
    dataset_name = tool_parameters.get("dataset_name", "")
    dataset_id = tool_parameters.get("dataset_id", "")
    if dataset_name:
        body["datasetName"] = dataset_name
    if dataset_id:
        body["datasetId"] = dataset_id

    session_ids = split_csv(tool_parameters.get("session_ids", ""))
    if session_ids:
        body["sessionIds"] = session_ids
    return body


def summarize_stages(result: Any) -> tuple[int, int]:
    """Return ``(completed, total)`` stage counts from an ``ImproveResult`` body."""
    stages = result.get("stages") if isinstance(result, dict) else None
    if not isinstance(stages, list):
        return 0, 0
    completed = sum(
        1
        for stage in stages
        if isinstance(stage, dict) and stage.get("status") in ("completed", "already_completed")
    )
    return completed, len(stages)


class ImproveTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)

        if not tool_parameters.get("dataset_name") and not tool_parameters.get("dataset_id"):
            error_msg = "Either dataset_name or dataset_id must be provided"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
            return

        body = build_body(tool_parameters)

        try:
            with make_client() as client:
                response = client.post(
                    f"{base_url}/api/v1/improve",
                    json=body,
                    headers=authorize(client, self.runtime, json=True),
                    timeout=IMPROVE_TIMEOUT,
                )
                response.raise_for_status()
                result = parse_json(response)

                status = str(result.get("status", "completed"))
                completed, total = summarize_stages(result)
                label = tool_parameters.get("dataset_name") or tool_parameters.get("dataset_id")

                yield self.create_json_message(result)
                yield self.create_variable_message("status", status)
                yield self.create_variable_message("stages_completed", completed)
                yield self.create_variable_message("stages_total", total)
                if status == "running":
                    summary = f"Improve started in the background for dataset '{label}'."
                else:
                    summary = (
                        f"Improve finished with status '{status}' for dataset '{label}': "
                        f"{completed} of {total} stage(s) completed."
                    )
                yield self.create_text_message(summary)
        except Exception as e:
            error_msg = error_text(e, "Failed to improve")
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
