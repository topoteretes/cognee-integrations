"""The low-level ingestion and dataset tools: request shapes and formatting."""

from conftest import Runtime, form_fields, invoke, variables


def test_dataset_form_fields_and_text_upload():
    from tools.ingestion import dataset_form_fields, text_upload

    assert dataset_form_fields("n", "", []) == {"datasetName": "n"}
    assert dataset_form_fields("", "id", ["a", "b"]) == {"datasetId": "id", "node_set": ["a", "b"]}
    assert dataset_form_fields("", "", []) == {}

    name, file, content_type = text_upload("héllo")
    assert name.startswith("data_") and name.endswith(".txt")
    assert file.read() == "héllo".encode()
    assert content_type == "text/plain"


def test_pipeline_dataset_and_first_data_id():
    from tools.ingestion import first_data_id, pipeline_dataset

    body = {
        "status": "PipelineRunCompleted",
        "dataset_id": "ds-1",
        "dataset_name": "notes",
        "data_ingestion_info": [{"data_id": "a"}, {"data_id": "b"}],
    }
    assert pipeline_dataset(body, "fallback", "") == ("notes", "ds-1")
    assert pipeline_dataset([body], "fallback", "") == ("notes", "ds-1")
    assert pipeline_dataset({"status": "ok"}, "fallback", "fid") == ("fallback", "fid")
    assert first_data_id(body) == ("a", 2)
    assert first_data_id([body, {"data_ingestion_info": [{"data_id": "c"}]}]) == ("a", 3)
    assert first_data_id({"status": "ok"}) == ("", 0)


def test_add_data_invocation(fake_cognee):
    from tools.add_data import AddDataTool

    fake_cognee.on(
        "POST",
        "/api/v1/add",
        json_body={
            "status": "PipelineRunCompleted",
            "dataset_id": "ds-1",
            "dataset_name": "notes",
            "data_ingestion_info": [{"data_id": "d-1"}],
        },
    )
    messages = invoke(
        AddDataTool, Runtime(), {"text_data": "hello", "dataset_name": "notes", "node_set": "x, y"}
    )
    fields = form_fields(fake_cognee.calls("POST", "/api/v1/add")[0])
    assert fields["datasetName"] == ["notes"]
    assert fields["node_set"] == ["x", "y"]
    assert fields["data"] == ["hello"]
    out = variables(messages)
    assert out == {
        "dataset_name": "notes",
        "dataset_id": "ds-1",
        "data_id": "d-1",
        "items_count": 1,
    }


def test_update_data_invocation_uses_query_parameters(fake_cognee):
    from tools.update_data import UpdateDataTool

    fake_cognee.on("PATCH", "/api/v1/update", json_body={"status": "incremental"})
    messages = invoke(
        UpdateDataTool, Runtime(), {"dataset_id": "ds-1", "data_id": "d-1", "text_data": "new"}
    )
    request = fake_cognee.calls("PATCH", "/api/v1/update")[0]
    assert dict(request.url.params) == {"data_id": "d-1", "dataset_id": "ds-1"}
    assert form_fields(request)["data"] == ["new"]
    assert variables(messages)["succeeded"] is True


def test_get_datasets_formatting():
    from tools.get_datasets import format_datasets

    count, text = format_datasets([{"id": "1", "name": "notes", "createdAt": "2026-09-30"}])
    assert count == 1
    assert "1. notes (id: 1, created: 2026-09-30)" in text
    assert format_datasets([]) == (0, "No datasets found.")


def test_get_dataset_data_formatting():
    from tools.get_dataset_data import format_items

    count, text = format_items(
        [{"id": "d1", "name": "a.txt", "extension": "txt", "createdAt": "x"}]
    )
    assert count == 1
    assert "1. a.txt (id: d1, type: txt, created: x)" in text
    assert format_items({"unexpected": True}) == (0, "No data items found in this dataset.")


def test_search_body_carries_node_name_and_verbose():
    from tools.search import build_body

    body = build_body({"query": "q", "node_name": "people, places", "verbose": "true", "top_k": 3})
    assert body["nodeName"] == ["people", "places"]
    assert body["verbose"] is True
    assert body["topK"] == 3
    assert body["searchType"] == "GRAPH_COMPLETION"


def test_search_formats_results_and_verbose_entries():
    from tools.search import format_results

    body = [
        {
            "dataset_id": "x",
            "dataset_name": "notes",
            "search_result": "Cognee turns documents into AI memory.",
        },
        {
            "dataset_name": "empty",
            "search_result": "",
            "error": "no data has been added; add data and run cognify before searching.",
        },
    ]
    count, text = format_results(body)
    assert count == 2
    assert "1. [notes] Cognee turns documents into AI memory." in text
    assert "2. [empty] error: no data has been added" in text

    _, text = format_results(
        [{"dataset_name": "notes", "search_result": [{"text": "chunk one"}, "chunk two"]}]
    )
    assert "[notes] chunk one\nchunk two" in text

    verbose = [
        {
            "dataset_name": "notes",
            "text_result": "answer",
            "context_result": "ctx",
            "objects_result": [],
        }
    ]
    assert "1. [notes] answer" in format_results(verbose)[1]

    count, text = format_results(["a completion string", {"content": "legacy"}])
    assert (count, "2. legacy" in text) == (2, True)
    assert format_results({"unexpected": True}) == (1, "{'unexpected': True}")
