"""Request shapes, result formatting and mocked invocations of the memory tools."""

import httpx
import pytest
from conftest import Runtime, invoke, json_body, text_of, variables

# --- shared client helpers -----------------------------------------------------


def test_split_csv_and_as_bool():
    from tools.cognee_client import as_bool, split_csv

    assert split_csv(" a, b ,,c ") == ["a", "b", "c"]
    assert split_csv("") == []
    assert split_csv(None) == []
    assert as_bool("true") is True
    assert as_bool("True") is True
    assert as_bool("false") is False
    assert as_bool(True) is True
    assert as_bool(None) is False


def test_error_text_prefers_the_server_body():
    from tools.cognee_client import error_text

    request = httpx.Request("POST", "http://x/api/v1/recall")
    response = httpx.Response(404, text='{"detail":"No datasets found."}', request=request)
    exc = httpx.HTTPStatusError("nope", request=request, response=response)
    assert (
        error_text(exc, "Recall failed") == 'Cognee API error 404: {"detail":"No datasets found."}'
    )
    assert error_text(RuntimeError("boom"), "Recall failed") == "Recall failed: boom"


def test_parse_json_never_turns_garbage_into_success():
    from tools.cognee_client import parse_json

    ok = httpx.Response(200, json={"status": "completed"})
    assert parse_json(ok) == {"status": "completed"}

    html = httpx.Response(200, text="<html>gateway timeout</html>")
    with pytest.raises(ValueError, match="non-JSON response \\(HTTP 200\\): <html>gateway timeout"):
        parse_json(html)

    empty = httpx.Response(200, text="")
    with pytest.raises(ValueError, match="non-JSON response \\(HTTP 200\\)$"):
        parse_json(empty)


# --- remember --------------------------------------------------------------------


def test_remember_form_uses_raw_data_and_omits_unset_fields():
    from tools.remember import build_form

    form = build_form({"text_data": "Einstein was born in Ulm.", "dataset_name": "notes"})
    assert form == {
        "raw_data": ["Einstein was born in Ulm."],
        "run_in_background": "true",
        "datasetName": "notes",
    }


def test_remember_form_carries_every_option():
    from tools.remember import build_form

    form = build_form(
        {
            "text_data": "t",
            "dataset_id": "3f2b",
            "node_set": "people, places",
            "session_id": "conv-1",
            "custom_prompt": "Extract people.",
            "self_improvement": "false",
        }
    )
    assert form["datasetId"] == "3f2b"
    assert form["node_set"] == ["people", "places"]
    assert form["session_id"] == "conv-1"
    assert form["custom_prompt"] == "Extract people."
    assert form["self_improvement"] == "false"
    assert "datasetName" not in form


def test_remember_form_leaves_self_improvement_to_the_server_by_default():
    from tools.remember import build_form

    form = build_form({"text_data": "t", "dataset_name": "n", "self_improvement": "default"})
    assert "self_improvement" not in form


# --- recall ----------------------------------------------------------------------


def test_recall_body_pins_stream_false_and_omits_auto_values():
    from tools.recall import build_body

    body = build_body({"query": "Where was Einstein born?"})
    assert body == {
        "query": "Where was Einstein born?",
        "topK": 15,
        "onlyContext": False,
        "includeReferences": False,
        "stream": False,
    }


def test_recall_body_carries_scope_session_and_datasets():
    from tools.recall import build_body

    body = build_body(
        {
            "query": "q",
            "datasets": "a,b",
            "dataset_ids": "id1",
            "session_id": "conv-1",
            "scope": "session_first",
            "search_type": "GRAPH_COMPLETION",
            "system_prompt": "Be brief.",
            "top_k": 3,
            "only_context": "true",
            "include_references": "true",
        }
    )
    assert body["datasets"] == ["a", "b"]
    assert body["datasetIds"] == ["id1"]
    assert body["sessionId"] == "conv-1"
    assert body["scope"] == "session_first"
    assert body["searchType"] == "GRAPH_COMPLETION"
    assert body["systemPrompt"] == "Be brief."
    assert body["topK"] == 3
    assert body["onlyContext"] is True
    assert body["includeReferences"] is True
    assert body["stream"] is False


def test_recall_formats_mixed_sources_and_picks_the_graph_answer():
    from tools.recall import format_results

    body = [
        {"source": "session", "question": "Who?", "answer": "Einstein.", "time": "t"},
        {
            "source": "session_context",
            "content": "User prefers short answers",
            "context_profile": "qa",
        },
        {
            "source": "graph",
            "text": "Einstein was born in Ulm.",
            "dataset_name": "notes",
            "kind": "completion",
            "search_type": "HYBRID_COMPLETION",
        },
    ]
    count, text, answer = format_results(body)
    assert count == 3
    assert "1. Q: Who?\nA: Einstein." in text
    assert "2. User prefers short answers" in text
    assert "3. [notes] Einstein was born in Ulm." in text
    assert answer == "Einstein was born in Ulm."


def test_recall_formats_the_warm_up_marker():
    from tools.recall import format_results

    body = [
        {
            "source": "system",
            "status": "memory_warming_up",
            "text": "Memory is still warming up: no knowledge graph data exists yet.",
            "datapoint_count": 0,
            "threshold": 10,
        }
    ]
    count, text, answer = format_results(body)
    assert count == 1
    assert "[memory_warming_up] Memory is still warming up" in text
    assert answer.startswith("Memory is still warming up")


def test_recall_handles_empty_and_non_list_bodies():
    from tools.recall import format_results

    assert format_results([]) == (0, "No results.", "")
    assert format_results("plain") == (1, "plain", "plain")


# --- forget ----------------------------------------------------------------------


def test_forget_body_prefers_dataset_id_and_carries_data_id():
    from tools.forget import build_body, describe

    body = build_body({"dataset_name": "notes", "dataset_id": "3f2b", "data_id": "d1"})
    assert body == {"memoryOnly": False, "everything": False, "datasetId": "3f2b", "dataId": "d1"}
    assert describe(body) == "data item 'd1' in dataset '3f2b'"


def test_forget_body_memory_only_by_name():
    from tools.forget import build_body, describe

    body = build_body({"dataset_name": "notes", "memory_only": "true"})
    assert body == {"memoryOnly": True, "everything": False, "dataset": "notes"}
    assert describe(body) == "the memory of dataset 'notes'"


def test_forget_body_everything_needs_an_explicit_true():
    from tools.forget import build_body, describe

    assert build_body({"dataset_name": "n"})["everything"] is False
    body = build_body({"everything": "true"})
    assert body["everything"] is True
    assert describe(body) == "all datasets and data owned by this user"


# --- remember entry --------------------------------------------------------------


def test_remember_entry_body_is_snake_case_qa():
    from tools.remember_entry import build_body

    body = build_body(
        {
            "session_id": "conv-1",
            "question": "Who?",
            "answer": "Einstein.",
            "context": "notes",
            "dataset_name": "main_dataset",
        }
    )
    assert body == {
        "entry": {"type": "qa", "question": "Who?", "answer": "Einstein.", "context": "notes"},
        "session_id": "conv-1",
        "dataset_name": "main_dataset",
    }


def test_remember_entry_body_omits_empty_optionals():
    from tools.remember_entry import build_body

    body = build_body({"session_id": "s", "question": "q", "answer": "a", "dataset_id": "3f2b"})
    assert body == {
        "entry": {"type": "qa", "question": "q", "answer": "a"},
        "session_id": "s",
        "dataset_id": "3f2b",
    }


# --- improve ---------------------------------------------------------------------


def test_improve_body_and_stage_summary():
    from tools.improve import build_body, summarize_stages

    body = build_body(
        {
            "dataset_name": "notes",
            "session_ids": "s1, s2",
            "run_in_background": "true",
            "build_global_context_index": "true",
        }
    )
    assert body == {
        "runInBackground": True,
        "buildGlobalContextIndex": True,
        "datasetName": "notes",
        "sessionIds": ["s1", "s2"],
    }

    result = {
        "status": "completed",
        "stages": [
            {"name": "feedback_weights", "status": "skipped", "reason": "no_session_ids"},
            {"name": "triplet_enrichment", "status": "completed"},
            {"name": "global_context_index", "status": "already_completed"},
        ],
    }
    assert summarize_stages(result) == (2, 3)
    assert summarize_stages({"status": "running"}) == (0, 0)


# --- mocked invocations through the Tool classes ---------------------------------


def test_recall_invocation_with_mocked_http(fake_cognee):
    from tools.recall import RecallTool

    fake_cognee.on(
        "POST",
        "/api/v1/recall",
        json_body=[
            {
                "source": "graph",
                "text": "Ulm.",
                "kind": "completion",
                "search_type": "HYBRID_COMPLETION",
            }
        ],
    )
    messages = invoke(RecallTool, Runtime(), {"query": "Where?", "session_id": "conv-1"})

    request = fake_cognee.calls("POST", "/api/v1/recall")[0]
    assert request.headers["X-Api-Key"] == "key-1"
    assert request.headers["Accept"] == "application/json"
    body = json_body(request)
    assert body["sessionId"] == "conv-1"
    assert body["stream"] is False

    out = variables(messages)
    assert out["results_count"] == 1
    assert out["answer"] == "Ulm."
    assert "1. Ulm." in out["results_text"]


def test_forget_invocation_reports_a_malformed_body_as_failure(fake_cognee):
    from tools.forget import ForgetTool

    fake_cognee.on(
        "POST", "/api/v1/forget", lambda r: httpx.Response(200, text="<html>oops</html>")
    )
    messages = invoke(ForgetTool, Runtime(), {"dataset_name": "notes"})
    assert variables(messages)["succeeded"] is False
    assert "non-JSON response" in text_of(messages)


def test_forget_invocation_success(fake_cognee):
    from tools.forget import ForgetTool

    fake_cognee.on("POST", "/api/v1/forget", json_body={"deleted": True})
    messages = invoke(ForgetTool, Runtime(), {"dataset_id": "ds-1", "data_id": "d-1"})
    assert json_body(fake_cognee.calls("POST", "/api/v1/forget")[0]) == {
        "memoryOnly": False,
        "everything": False,
        "datasetId": "ds-1",
        "dataId": "d-1",
    }
    assert variables(messages)["succeeded"] is True
    assert "Forgot data item 'd-1' in dataset 'ds-1'." in text_of(messages)


def test_delete_dataset_invocation_with_login_credentials(fake_cognee):
    from conftest import login_ok
    from tools.delete_dataset import DeleteDatasetTool

    fake_cognee.on("POST", "/api/v1/auth/login", login_ok)
    fake_cognee.on("DELETE", "/api/v1/datasets/ds-1", lambda r: httpx.Response(200))
    runtime = Runtime(api_key="", user_email="u@e", user_password="p")
    messages = invoke(DeleteDatasetTool, runtime, {"dataset_id": "ds-1"})
    delete = fake_cognee.calls("DELETE", "/api/v1/datasets/ds-1")[0]
    assert delete.headers["Authorization"] == "Bearer tok"
    assert variables(messages)["succeeded"] is True
