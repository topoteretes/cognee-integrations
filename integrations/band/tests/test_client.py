"""Tests for the Cognee HTTP client: request shapes and error contract."""

import io
import json
import urllib.error

import pytest
from cognee_band.client import UNREACHABLE, CogneeClient
from cognee_band.config import CogneeSettings


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def make_client(responses, captured):
    """Client with a fake opener. ``responses`` is a list of bytes bodies or
    exceptions, consumed per request; requests are appended to ``captured``."""

    def opener(req, timeout=None):
        captured.append((req, timeout))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    settings = CogneeSettings(base_url="http://cognee.test", api_key="ck_test", dataset="ds")
    return CogneeClient(settings, opener=opener)


def body_of(req):
    return json.loads(req.data.decode("utf-8"))


def test_recall_request_shape_and_result():
    captured = []
    client = make_client([json.dumps([{"text": "hit"}]).encode()], captured)
    result = client.recall("what is x", session_id="band-room1")
    assert result == [{"text": "hit"}]
    req, _ = captured[0]
    assert req.full_url == "http://cognee.test/api/v1/recall"
    body = body_of(req)
    assert body["query"] == "what is x"
    assert body["datasets"] == ["ds"]
    assert body["session_id"] == "band-room1"
    assert body["only_context"] is True
    assert req.headers["X-api-key"] == "ck_test"


def test_recall_empty_list_is_authoritative():
    client = make_client([b"[]"], [])
    assert client.recall("q") == []


def test_recall_http_error_is_error_envelope_not_empty():
    err = urllib.error.HTTPError("u", 401, "nope", {}, io.BytesIO(b""))
    client = make_client([err], [])
    result = client.recall("q")
    assert isinstance(result, dict)
    assert result["status"] == 401
    assert "unauthorized" in result["error"]


def test_recall_transport_failure_is_unreachable():
    client = make_client([OSError("refused")], [])
    assert client.recall("q") == UNREACHABLE


def test_store_qa_entry_shape():
    captured = []
    client = make_client([b"{}"], captured)
    client.store_qa("Q?", "A.", session_id="band-r1", context="ctx")
    req, _ = captured[0]
    assert req.full_url == "http://cognee.test/api/v1/remember/entry"
    body = body_of(req)
    assert body["dataset_name"] == "ds"
    assert body["session_id"] == "band-r1"
    assert body["entry"] == {"type": "qa", "question": "Q?", "answer": "A.", "context": "ctx"}


def test_store_qa_without_session_is_noop():
    captured = []
    client = make_client([], captured)
    assert client.store_qa("Q?", "A.", session_id="") is None
    assert captured == []


def test_store_trace_entry_shape():
    captured = []
    client = make_client([b"{}"], captured)
    client.store_trace("cognee_search", {"q": "x"}, "result", session_id="band-r1")
    body = body_of(captured[0][0])
    entry = body["entry"]
    assert entry["type"] == "trace"
    assert entry["origin_function"] == "cognee_search"
    assert entry["generate_feedback_with_llm"] is False


def test_improve_request_shape():
    captured = []
    client = make_client([json.dumps({"dataset_id": "d1"}).encode()], captured)
    result = client.improve("band-r1")
    assert result["ok"] is True
    req, _ = captured[0]
    assert req.full_url == "http://cognee.test/api/v1/improve"
    body = body_of(req)
    assert body == {"dataset_name": "ds", "session_ids": ["band-r1"], "run_in_background": True}


def test_improve_unreachable_reports_not_ok():
    client = make_client([OSError("refused")], [])
    assert client.improve("band-r1") == {"ok": False, "error": "unreachable"}


def test_remember_multipart():
    captured = []
    client = make_client([b"{}"], captured)
    result = client.remember("remember me", node_set="band_memory")
    assert result == {"ok": True}
    req, _ = captured[0]
    assert req.full_url == "http://cognee.test/api/v1/remember"
    raw = req.data.decode("utf-8")
    assert 'name="datasetName"' in raw and "ds" in raw
    assert "remember me" in raw
    assert 'name="run_in_background"' in raw


@pytest.mark.parametrize("bad", [b"not json {"])
def test_malformed_json_is_error_not_results(bad):
    client = make_client([bad], [])
    result = client.recall("q")
    assert isinstance(result, dict) and result.get("error")
