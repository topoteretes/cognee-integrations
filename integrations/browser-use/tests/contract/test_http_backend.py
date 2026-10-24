"""HTTP contract tests against bodies captured from cognee 1.6.5 (see fixtures/README.md)."""

import httpx
import pytest
from cognee_integration_browser_use import CogneeError, CogneeHttpBackend
from tests.conftest import API_KEY, BASE_URL, DATASET_ID, FakeCognee, fixture


def backend(transport, **kw) -> CogneeHttpBackend:
    return CogneeHttpBackend(
        base_url=BASE_URL + "/", api_key=API_KEY, request_timeout_s=2.0, transport=transport, **kw
    )


def static(status: int, body):
    return httpx.MockTransport(lambda request: httpx.Response(status, json=body))


async def test_remember_uploads_named_file_with_background_flag():
    fake = FakeCognee()
    async with backend(fake.transport) as client:
        result = await client.remember(
            dataset="browser_use_probe", filename="browser-x.txt", text="hello"
        )
    assert result["status"] == "running"
    assert result["dataset_id"] == DATASET_ID
    call = next(c for c in fake.calls if c["path"] == "/api/v1/remember")
    raw = call["raw"].decode()
    assert 'filename="browser-x.txt"' in raw
    assert 'name="run_in_background"\r\n\r\ntrue' in raw
    assert call["headers"]["x-api-key"] == API_KEY


async def test_recall_states_graph_scope_and_explicit_datasets():
    fake = FakeCognee()
    async with backend(fake.transport) as client:
        items = await client.recall(query="login", datasets=["browser_use_probe"], top_k=3)
        assert items[0]["metadata"]["data_id"] == "d03f564c-ff7b-4810-ac63-c27b1c37138a"
        call = next(c for c in fake.calls if c["path"] == "/api/v1/recall")
        assert call["body"] == {
            "query": "login",
            "datasets": ["browser_use_probe"],
            "scope": ["graph"],
            "search_type": "CHUNKS",
            "top_k": 3,
        }
        with pytest.raises(CogneeError, match="at least one dataset"):
            await client.recall(query="x", datasets=[], top_k=1)


async def test_auth_and_not_found_errors_are_classified():
    async with backend(static(401, fixture("unauthorized.json")["body"])) as client:
        with pytest.raises(CogneeError) as exc:
            await client.list_datasets()
        assert exc.value.is_auth and exc.value.status == 401 and not exc.value.outcome_uncertain
        assert "Authentication required" in exc.value.detail
    async with backend(static(404, fixture("recall-dataset-not-found.json")["body"])) as client:
        with pytest.raises(CogneeError) as exc:
            await client.recall(query="x", datasets=["nope"], top_k=1)
        assert exc.value.is_not_found and not exc.value.is_no_searchable_memory
    body = {
        "detail": (
            "NoDataError: No searchable memory in dataset 'x' (id: y): no data has been "
            "added; add data and run cognify before searching. (Status code: 404)"
        )
    }
    async with backend(static(404, body)) as client:
        with pytest.raises(CogneeError) as exc:
            await client.recall(query="x", datasets=["x"], top_k=1)
        assert exc.value.is_no_searchable_memory


async def test_error_bearing_2xx_rate_limit_and_server_error():
    async with backend(static(200, {"error": "An error occurred during processing."})) as client:
        with pytest.raises(CogneeError, match="answered an error"):
            await client.ensure_dataset("x")
    attempts = {"n": 0}

    def flaky(request):
        attempts["n"] += 1
        return httpx.Response(429 if attempts["n"] == 1 else 200, json=[])

    async with backend(httpx.MockTransport(flaky)) as client:
        assert await client.list_datasets() == []
    assert attempts["n"] == 2
    async with backend(static(503, {"detail": "down"})) as client:
        with pytest.raises(CogneeError) as exc:
            await client.remember(dataset="d", filename="f.txt", text="t")
        assert exc.value.is_server_error and not exc.value.outcome_uncertain


async def test_write_timeout_is_uncertain_and_not_retried():
    attempts = {"n": 0}

    def slow(request):
        attempts["n"] += 1
        raise httpx.ReadTimeout("slow")

    async with backend(httpx.MockTransport(slow)) as client:
        with pytest.raises(CogneeError) as exc:
            await client.remember(dataset="d", filename="f.txt", text="t")
        assert exc.value.kind == "timeout" and exc.value.outcome_uncertain
    assert attempts["n"] == 1


async def test_reads_retry_once_on_network_failure_within_deadline():
    attempts = {"n": 0}

    def flaky(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=[{"id": DATASET_ID, "name": "x"}])

    async with backend(httpx.MockTransport(flaky)) as client:
        assert (await client.list_datasets())[0]["name"] == "x"
    assert attempts["n"] == 2


async def test_redirects_are_not_followed_and_oversized_bodies_refused():
    async with backend(
        httpx.MockTransport(
            lambda r: httpx.Response(307, headers={"Location": "https://elsewhere.example/"})
        )
    ) as client:
        with pytest.raises(CogneeError) as exc:
            await client.list_datasets()
        assert exc.value.kind == "redirect"
    big = httpx.MockTransport(
        lambda r: httpx.Response(
            200, content=b"x" * 2048, headers={"Content-Type": "application/json"}
        )
    )
    async with backend(big, max_response_bytes=1024) as client:
        with pytest.raises(CogneeError, match="too large"):
            await client.list_datasets()


async def test_status_listing_and_targeted_forget_contract():
    fake = FakeCognee()
    fake.data.append(
        {
            "id": "d03f564c-ff7b-4810-ac63-c27b1c37138a",
            "name": "memory-abc123",
            "datasetId": DATASET_ID,
        }
    )
    async with backend(fake.transport) as client:
        assert await client.dataset_status(DATASET_ID) == "DATASET_PROCESSING_COMPLETED"
        item = await client.find_data_item(DATASET_ID, "memory-abc123")
        assert item["id"] == "d03f564c-ff7b-4810-ac63-c27b1c37138a"
        assert await client.find_data_item(DATASET_ID, "missing") is None
        result = await client.forget_document(dataset_id=DATASET_ID, data_id=item["id"])
        assert result["status"] == "success"
        assert await client.find_data_item(DATASET_ID, "memory-abc123") is None
        with pytest.raises(CogneeError, match="not a UUID"):
            await client.forget_document(dataset_id="nope", data_id=item["id"])
        with pytest.raises(CogneeError, match="not a UUID"):
            await client.dataset_status("../../etc")


async def test_duplicate_content_is_one_document_server_side():
    fake = FakeCognee()
    async with backend(fake.transport) as client:
        await client.remember(dataset="browser_use_probe", filename="browser-same.txt", text="same")
        await client.remember(dataset="browser_use_probe", filename="browser-same.txt", text="same")
    assert len(fake.data) == 1


async def test_health_tolerates_any_status():
    async with backend(static(503, {"status": "degraded"})) as client:
        report = await client.health()
    assert report["reachable"] and report["status"] == 503
