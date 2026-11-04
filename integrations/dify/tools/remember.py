import hashlib
import time
from collections.abc import Generator
from typing import Any

import httpx
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

REMEMBER_TIMEOUT = 21600


def build_form(tool_parameters: dict[str, Any]) -> dict[str, Any]:
    """Multipart/form fields for ``POST /api/v1/remember``.

    The text goes in ``raw_data`` so the server names the document by its
    content hash; no upload filename is involved. The request is always
    submitted as a background run: a blocking remember can exceed the HTTP
    timeout of Cognee Cloud's gateway on larger inputs. Waiting for the build
    is the plugin's job (see ``wait_for_build``), unless the caller asked to
    return immediately.
    """
    form: dict[str, Any] = {
        "raw_data": [tool_parameters["text_data"]],
        "run_in_background": "true",
    }

    dataset_name = tool_parameters.get("dataset_name", "")
    dataset_id = tool_parameters.get("dataset_id", "")
    if dataset_name:
        form["datasetName"] = dataset_name
    if dataset_id:
        form["datasetId"] = dataset_id

    node_set = split_csv(tool_parameters.get("node_set", ""))
    if node_set:
        form["node_set"] = node_set

    session_id = tool_parameters.get("session_id", "")
    if session_id:
        form["session_id"] = session_id

    custom_prompt = tool_parameters.get("custom_prompt", "")
    if custom_prompt:
        form["custom_prompt"] = custom_prompt

    self_improvement = tool_parameters.get("self_improvement", "default")
    if self_improvement in ("true", "false"):
        form["self_improvement"] = self_improvement

    return form


# --- waiting for the build -----------------------------------------------------------
#
# A background remember answers before anything is ingested and carries no
# pipeline run id, and the dataset-level status still shows the previous run
# as completed, so neither can be trusted on its own. Two facts the server
# does expose make completion provable:
#
# * ``GET /api/v1/datasets/graph-summary`` reports each dataset's latest
#   cognify ``pipelineRunId``. A value different from the one recorded before
#   the call is this request's run; its ``GET /api/v1/datasets/status`` then
#   says whether it finished.
# * Raw text is stored under the name ``text_<md5 of the utf-8 text>.txt``.
#   When that item already exists before the call the content is deduplicated
#   and nothing new will be built: the memory already holds it.
#
# When neither fact can be established before the deadline the tool reports
# ``running`` instead of guessing.

POLL_INTERVAL = 3.0
WAIT_TIMEOUT = 3600.0
STATUS_OF = {
    "DATASET_PROCESSING_COMPLETED": "completed",
    "DATASET_PROCESSING_ERRORED": "errored",
}


def text_item_name(text: str) -> str:
    return "text_" + hashlib.md5(text.encode("utf-8")).hexdigest() + ".txt"


def resolve_dataset_id(client: httpx.Client, base_url: str, headers: dict, name: str) -> str:
    """Dataset id for a name the caller owns, or "" when it does not exist yet."""
    response = client.get(f"{base_url}/api/v1/datasets", headers=headers, timeout=60)
    response.raise_for_status()
    for dataset in response.json():
        if isinstance(dataset, dict) and dataset.get("name") == name:
            return str(dataset.get("id", ""))
    return ""


def latest_run_id(
    client: httpx.Client, base_url: str, headers: dict, dataset_id: str
) -> str | None:
    """The dataset's latest cognify pipeline run id, or None when it has none yet."""
    response = client.get(
        f"{base_url}/api/v1/datasets/graph-summary",
        params={"dataset_ids": dataset_id},
        headers=headers,
        timeout=60,
    )
    if response.status_code == 409:
        # The one transient answer: "summary could not be built" on a dataset
        # whose first run has not produced a graph yet. Anything else is a real
        # failure (permissions, server error, or a server without this route)
        # and must not be mistaken for "no run yet".
        return None
    if response.status_code == 404:
        raise RuntimeError(
            "This Cognee server has no GET /api/v1/datasets/graph-summary route, which "
            "Remember needs to confirm the build; set Run in Background to true and poll "
            "GET /api/v1/datasets/status yourself."
        )
    response.raise_for_status()
    for row in parse_json(response) or []:
        if (
            isinstance(row, dict)
            and str(row.get("datasetId") or row.get("dataset_id")) == dataset_id
        ):
            run_id = row.get("pipelineRunId") or row.get("pipeline_run_id")
            return str(run_id) if run_id else None
    return None


def dataset_status(
    client: httpx.Client, base_url: str, headers: dict, dataset_id: str
) -> str | None:
    """Raw status of the dataset's latest cognify run (``DATASET_PROCESSING_*``), or None."""
    response = client.get(
        f"{base_url}/api/v1/datasets/status",
        params={"dataset": dataset_id},
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    raw = body.get(dataset_id) if isinstance(body, dict) else None
    if isinstance(raw, dict):
        raw = raw.get("cognify_pipeline") or next(iter(raw.values()), None)
    return str(raw) if raw is not None else None


def has_text_item(
    client: httpx.Client, base_url: str, headers: dict, dataset_id: str, text: str
) -> bool:
    """True when the dataset already holds a raw-text item with this exact content."""
    wanted = text_item_name(text)
    response = client.get(
        f"{base_url}/api/v1/datasets/{dataset_id}/data", headers=headers, timeout=60
    )
    if response.is_error:
        return False
    return any(
        isinstance(item, dict) and item.get("name") == wanted for item in response.json() or []
    )


def wait_for_build(
    client: httpx.Client,
    base_url: str,
    headers: dict,
    dataset_id: str,
    baseline_run_id: str | None,
    *,
    sleep=time.sleep,
    now=time.monotonic,
    timeout: float = WAIT_TIMEOUT,
) -> str:
    """Poll until a cognify run newer than ``baseline_run_id`` reaches a terminal state.

    Returns ``"completed"``, ``"errored"``, or ``"running"`` when the deadline
    passes without proof. A completed status is only accepted together with a
    run id that differs from the baseline, so the previous run's completion is
    never mistaken for this one.
    """
    deadline = now() + timeout
    while True:
        run_id = latest_run_id(client, base_url, headers, dataset_id)
        if run_id is not None and run_id != baseline_run_id:
            outcome = STATUS_OF.get(dataset_status(client, base_url, headers, dataset_id) or "")
            if outcome:
                return outcome
        if now() >= deadline:
            return "running"
        sleep(POLL_INTERVAL)


def summarize(result: dict[str, Any], *, waited: bool, deduplicated: bool) -> str:
    status = str(result.get("status", "completed"))
    dataset_name = result.get("dataset_name", "")
    dataset_id = result.get("dataset_id", "")
    if deduplicated:
        return (
            f"This text is already remembered in dataset '{dataset_name}' (id: {dataset_id}); "
            "nothing new to build, the memory is ready to recall."
        )
    if status == "running":
        if waited:
            return (
                f"The memory for dataset '{dataset_name}' (id: {dataset_id}) was still being "
                "built when the wait timed out; it is not ready to recall yet."
            )
        return (
            f"Remember started in the background for dataset '{dataset_name}' (id: {dataset_id}). "
            "Poll GET /api/v1/datasets/status before recalling."
        )
    if status == "errored":
        return (
            f"Remember failed while building the memory for dataset '{dataset_name}' "
            f"(id: {dataset_id}): {result.get('error') or 'see server logs'}"
        )
    return (
        f"Remembered the text in dataset '{dataset_name}' (id: {dataset_id}); "
        "the memory is ready to recall."
    )


class RememberTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        base_url = base_url_of(self.runtime)

        if not tool_parameters.get("dataset_name") and not tool_parameters.get("dataset_id"):
            error_msg = "Either dataset_name or dataset_id must be provided"
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
            return

        form = build_form(tool_parameters)
        wait = not as_bool(tool_parameters.get("run_in_background", "false"))
        text = tool_parameters["text_data"]

        try:
            with make_client() as client:
                headers = authorize(client, self.runtime)

                # Evidence recorded before the call: the dataset's current run id
                # and whether this exact text is already stored in it.
                baseline_run_id: str | None = None
                deduplicated = False
                if wait:
                    known_id = tool_parameters.get("dataset_id", "") or resolve_dataset_id(
                        client, base_url, headers, tool_parameters.get("dataset_name", "")
                    )
                    if known_id:
                        baseline_run_id = latest_run_id(client, base_url, headers, known_id)
                        deduplicated = has_text_item(client, base_url, headers, known_id, text)

                response = client.post(
                    f"{base_url}/api/v1/remember",
                    headers=headers,
                    data=form,
                    timeout=REMEMBER_TIMEOUT,
                )
                response.raise_for_status()
                result = parse_json(response)
                result.setdefault("dataset_id", tool_parameters.get("dataset_id", ""))
                result.setdefault("dataset_name", tool_parameters.get("dataset_name", ""))
                dataset_id = str(result.get("dataset_id") or "")

                waited = False
                if wait and dataset_id:
                    if deduplicated:
                        result["status"] = "completed"
                    elif result.get("status") == "running":
                        result["status"] = wait_for_build(
                            client, base_url, headers, dataset_id, baseline_run_id
                        )
                        waited = True
                status = str(result.get("status", "completed"))

                yield self.create_json_message(result)
                yield self.create_variable_message("status", status)
                yield self.create_variable_message("dataset_id", dataset_id)
                yield self.create_variable_message(
                    "dataset_name", str(result.get("dataset_name") or "")
                )
                yield self.create_variable_message(
                    "pipeline_run_id", str(result.get("pipeline_run_id") or "")
                )
                yield self.create_variable_message(
                    "items_processed", int(result.get("items_processed") or 0)
                )
                yield self.create_text_message(
                    summarize(result, waited=waited, deduplicated=deduplicated)
                )
        except Exception as e:
            error_msg = error_text(e, "Failed to remember")
            yield self.create_json_message({"error": error_msg})
            yield self.create_text_message(error_msg)
