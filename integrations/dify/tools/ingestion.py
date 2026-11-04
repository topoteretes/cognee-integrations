"""Small helpers shared by the ingestion tools (Add Data, Add File, Update Data).

Each tool keeps its own endpoint, validation, filenames and output variables;
these only remove the duplicated form fields, text upload tuple and the
dataset lookup in a pipeline-run response.
"""

import io
import uuid
from typing import Any


def dataset_form_fields(dataset_name: str, dataset_id: str, node_set: list[str]) -> dict[str, Any]:
    """The ``datasetName`` / ``datasetId`` / ``node_set`` multipart fields the add routes take."""
    fields: dict[str, Any] = {}
    if dataset_name:
        fields["datasetName"] = dataset_name
    if dataset_id:
        fields["datasetId"] = dataset_id
    if node_set:
        fields["node_set"] = node_set
    return fields


def text_upload(text: str) -> tuple[str, io.BytesIO, str]:
    """A ``(filename, file, content type)`` tuple uploading ``text`` as a .txt file.

    The random name keeps every upload a new document; Cognee refuses an upload
    whose name matches a stored document with different content (HTTP 409).
    """
    return (f"data_{uuid.uuid4().hex[:8]}.txt", io.BytesIO(text.encode("utf-8")), "text/plain")


def first_run(result: Any) -> dict[str, Any]:
    """The first ``PipelineRunInfo`` dict of an add response (a dict or a list of them)."""
    run = result[0] if isinstance(result, list) and result else result
    return run if isinstance(run, dict) else {}


def pipeline_dataset(result: Any, fallback_name: str, fallback_id: str) -> tuple[str, str]:
    """``(dataset_name, dataset_id)`` from an add response, falling back to the request values."""
    run = first_run(result)
    return (
        str(run.get("dataset_name") or fallback_name or ""),
        str(run.get("dataset_id") or fallback_id or ""),
    )


def first_data_id(result: Any) -> tuple[str, int]:
    """``(first data id, item count)`` from the ``data_ingestion_info`` of an add response."""
    data_ids: list[str] = []
    for run in result if isinstance(result, list) else [result]:
        if not isinstance(run, dict):
            continue
        for info in run.get("data_ingestion_info") or []:
            if isinstance(info, dict) and info.get("data_id"):
                data_ids.append(str(info["data_id"]))
    return (data_ids[0] if data_ids else ""), len(data_ids)


def as_json_message(result: Any, key: str = "result") -> dict[str, Any]:
    """Dify's JSON message wants a mapping; wrap list bodies under ``key``."""
    return result if isinstance(result, dict) else {key: result}
