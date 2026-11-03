import asyncio

import pytest
from browser_use import ActionResult, Tools
from cognee_integration_browser_use import MemoryScope, ReceiptStatus, RecordKind, Verification
from tests.conftest import FakeAgent, FakeHistory, fixture, make_memory

SCOPE = MemoryScope(tenant_id="acme", user_id="buyer-1")


async def test_run_requires_scope_or_dataset(fake):
    async with make_memory(fake) as memory:
        with pytest.raises(Exception, match="trusted MemoryScope"):
            memory.new_run(task="x")
        run = memory.new_run(task="x", scope=SCOPE)
        assert run.dataset == SCOPE.dataset_name()
        assert run.scope_key == SCOPE.key
    async with make_memory(fake, dataset="browser_use_probe") as memory:
        run = memory.new_run(task="x")
        assert run.dataset == "browser_use_probe"


async def test_recall_context_is_bounded_and_instructions_mark_history(fake):
    fake.datasets.append(
        {"id": "9b8c7d6e-0000-4000-8000-000000000002", "name": SCOPE.dataset_name()}
    )
    fake.recall = lambda body: [
        {
            **fixture("recall-chunks.json")[0],
            "dataset_name": SCOPE.dataset_name(),
            "text": "Supplier A ships in 3 days. " * 40,
            "score": 0.9,
        },
        {"kind": "chunk", "text": "LEAKED", "score": 0.99, "dataset_name": "someone_elses"},
        {
            "kind": "chunk",
            "text": "second finding",
            "score": 0.2,
            "dataset_name": SCOPE.dataset_name(),
        },
    ]
    async with make_memory(fake, recall_max_chars=200, recall_top_k=5) as memory:
        run = memory.new_run(
            task="find supplier A lead time", scope=SCOPE, query_hints=["supplier-a.example"]
        )
        context = await run.recall_context()
        assert context.status == "ok"
        assert context.truncated is True
        assert all(hit.dataset == SCOPE.dataset_name() for hit in context.hits)
        assert "LEAKED" not in context.text
        assert sum(len(h.text) for h in context.hits) <= 201
        text = run.instructions(context)
        assert "historical evidence" in text.lower()
        assert "cognee_recall" in text
        assert context.hits[0].document_name == "memory-abc123"
        recall_call = next(c for c in fake.calls if c["path"] == "/api/v1/recall")
        assert recall_call["body"]["datasets"] == [SCOPE.dataset_name()]
        assert recall_call["body"]["scope"] == ["graph"]
        assert "supplier-a.example" in recall_call["body"]["query"]
        assert run.status.recall == "ok"


async def test_recall_degrades_to_empty_context_on_outage(fake):
    import httpx

    def boom(**_):
        raise httpx.ConnectError("down")

    fake.overrides["POST /api/v1/recall"] = boom
    fake.datasets.append(
        {"id": "9b8c7d6e-0000-4000-8000-000000000002", "name": SCOPE.dataset_name()}
    )
    async with make_memory(fake) as memory:
        run = memory.new_run(task="anything", scope=SCOPE)
        context = await run.recall_context()
        assert context.status == "unavailable"
        assert context.hits == []
        assert run.status.recall == "unavailable"
        assert run.status.recall_error
        assert "No stored research matched" in run.instructions(context)


async def test_recall_on_unknown_dataset_is_empty_not_unscoped(fake):
    async with make_memory(fake) as memory:
        run = memory.new_run(task="anything", scope=SCOPE)
        context = await run.recall_context()
        assert context.status == "empty"
        assert not [c for c in fake.calls if c["path"] == "/api/v1/recall"]


async def test_execute_composes_hooks_captures_and_returns_history(fake):
    order: list[str] = []

    async def caller_start(agent):
        order.append("caller_start")

    async def caller_end(agent):
        order.append("caller_end")

    agent = FakeAgent(steps=2)
    async with make_memory(fake) as memory:
        run = memory.new_run(task="find supplier A lead time", scope=SCOPE)
        history = await run.execute(
            agent, max_steps=10, on_step_start=caller_start, on_step_end=caller_end
        )
        assert history is agent.history
        assert order == ["caller_start", "caller_end", "caller_start", "caller_end"]
        assert run.status.steps_observed == 2
        assert run.status.capture == "accepted"
        receipt = run.status.receipts[0]
        assert receipt.status == ReceiptStatus.ACCEPTED
        assert receipt.kind == RecordKind.PROCEDURE.value
        upload = next(c for c in fake.calls if c["path"] == "/api/v1/remember")
        text = fake.data[0]["text"]
        assert "Supplier A ships in 3 days." in text
        assert "token=secret123" not in text
        assert "https://supplier-a.example/catalog" in text
        assert "Verification: agent_reported" in text
        assert "Run: " + run.run_id in text
        assert upload["headers"]["x-api-key"]
        # Second finalize is a no-op.
        assert await run.finalize(agent.history) is None


async def test_execute_preserves_original_exception_and_records_failed_attempt(fake):
    agent = FakeAgent(steps=3, fail_with=RuntimeError("browser crashed"))
    async with make_memory(fake) as memory:
        run = memory.new_run(task="t", scope=SCOPE)
        with pytest.raises(RuntimeError, match="browser crashed"):
            await run.execute(agent, max_steps=10)
        assert run.status.capture == "accepted"
        assert run.status.receipts[0].kind == RecordKind.FAILED_ATTEMPT.value
        assert "failed with RuntimeError" in fake.data[0]["text"]


async def test_cancellation_propagates_and_leaves_small_interrupted_record(fake):
    agent = FakeAgent(steps=3, hang=True)
    async with make_memory(fake) as memory:
        run = memory.new_run(task="t", scope=SCOPE)
        task = asyncio.create_task(run.execute(agent, max_steps=10))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert run.status.capture in {"accepted", "skipped"}
        if run.status.capture == "accepted":
            assert run.status.receipts[0].kind == RecordKind.INTERRUPTED_RUN.value


async def test_capture_failure_never_masks_browser_result(fake):
    import httpx

    def boom(**_):
        raise httpx.ConnectError("down")

    fake.overrides["POST /api/v1/remember"] = boom
    agent = FakeAgent(steps=1)
    async with make_memory(fake) as memory:
        run = memory.new_run(task="t", scope=SCOPE)
        history = await run.execute(agent)
        assert history is agent.history
        assert run.status.capture == "unknown"
        assert run.status.capture_error
        assert run.status.ok is False


async def test_capture_disabled_and_validator_and_summarizer(fake):
    async with make_memory(fake, capture_enabled=False) as memory:
        run = memory.new_run(task="t", scope=SCOPE)
        await run.execute(FakeAgent(steps=1))
        assert run.status.capture == "disabled"
        assert not [c for c in fake.calls if c["path"] == "/api/v1/remember"]

    async with make_memory(fake) as memory:
        run = memory.new_run(task="t", scope=SCOPE)

        async def summarizer(history):
            return "Lead time confirmed: 3 business days (validated against the order form)."

        await run.execute(FakeAgent(steps=1), validator=lambda h: True, summarizer=summarizer)
        text = fake.data[-1]["text"]
        assert "Verification: application_validated" in text
        assert "Lead time confirmed" in text


async def test_concurrent_runs_with_different_scopes_stay_separate(fake):
    other = MemoryScope(tenant_id="acme", user_id="buyer-2")
    async with make_memory(fake) as memory:
        runs = [memory.new_run(task="a", scope=SCOPE), memory.new_run(task="b", scope=other)]
        await asyncio.gather(
            *(
                run.execute(FakeAgent(steps=1, history=FakeHistory(final=f"result {i}")))
                for i, run in enumerate(runs)
            )
        )
        uploads = [c for c in fake.calls if c["path"] == "/api/v1/remember"]
        assert len(uploads) == 2
        names = {d["name"] for d in fake.datasets}
        assert SCOPE.dataset_name() in names and other.dataset_name() in names
        assert runs[0].status.receipts[0].dataset != runs[1].status.receipts[0].dataset
        assert runs[0].status.receipts[0].record_id != runs[1].status.receipts[0].record_id


async def test_recalled_text_is_excluded_from_capture(fake):
    history = FakeHistory(
        final=None,
        names=["cognee_recall", "scroll", "extract"],
        contents=["[cognee memory] historical evidence: old price 10", None, "price today 12"],
    )
    agent = FakeAgent(steps=1, history=history)
    async with make_memory(fake) as memory:
        run = memory.new_run(task="t", scope=SCOPE)
        await run.execute(agent)
        text = fake.data[0]["text"]
        assert "old price 10" not in text
        assert "price today 12" in text


async def test_memory_tools_register_into_real_tools_and_never_terminate(fake):
    fake.datasets.append(
        {"id": "9b8c7d6e-0000-4000-8000-000000000002", "name": SCOPE.dataset_name()}
    )
    fake.recall = lambda body: [
        {**fixture("recall-chunks.json")[0], "dataset_name": SCOPE.dataset_name()}
    ]
    async with make_memory(fake, remember_tool_enabled=True) as memory:
        run = memory.new_run(task="t", scope=SCOPE)
        tools = Tools()
        names = run.register_tools(tools)
        assert names == ["cognee_recall", "cognee_remember"]
        assert "cognee_recall" in tools.registry.registry.actions
        recall = tools.registry.registry.actions["cognee_recall"]
        params = recall.param_model(query="login decision")
        result: ActionResult = await recall.function(params=params)
        assert result.is_done is not True
        assert result.error is None
        assert "historical evidence" in result.extracted_content
        assert "passwordless" in result.extracted_content
        assert run.status.tool_calls == ["cognee_recall"]

        remember = tools.registry.registry.actions["cognee_remember"]
        result = await remember.function(
            params=remember.param_model(
                content="Supplier B minimum order is 50 units",
                source_url="https://b.example/terms?token=x",
            )
        )
        assert result.error is None
        assert "saved as rec_" in result.extracted_content
        text = fake.data[-1]["text"]
        assert "Verification: unverified" in text
        assert "token=x" not in text
        assert "https://b.example/terms" in text


async def test_memory_tool_errors_are_actionable_not_fatal(fake):
    import httpx

    def boom(**_):
        raise httpx.ConnectError("down")

    fake.overrides["POST /api/v1/recall"] = boom
    fake.datasets.append(
        {"id": "9b8c7d6e-0000-4000-8000-000000000002", "name": SCOPE.dataset_name()}
    )
    async with make_memory(fake) as memory:
        run = memory.new_run(task="t", scope=SCOPE)
        tools = Tools()
        assert run.register_tools(tools) == ["cognee_recall"]
        assert "cognee_remember" not in tools.registry.registry.actions
        recall = tools.registry.registry.actions["cognee_recall"]
        result = await recall.function(params=recall.param_model(query="q"))
        assert result.error and "memory lookup failed" in result.error
        assert result.is_done is not True


async def test_status_reconcile_and_forget_record(fake):
    async with make_memory(fake) as memory:
        run = memory.new_run(task="t", scope=SCOPE)
        await run.execute(FakeAgent(steps=1))
        receipt = run.status.receipts[0]
        fake.pipeline = "DATASET_PROCESSING_STARTED"
        assert (await memory.record_status(receipt.record_id)).status == ReceiptStatus.INDEXING
        fake.pipeline = "DATASET_PROCESSING_COMPLETED"
        ready = await memory.record_status(receipt.record_id)
        assert ready.status == ReceiptStatus.READY
        assert ready.data_id.startswith("d03f564c")
        assert await memory.record_status("rec_nope") is None

        denied = await memory.forget_record(receipt.record_id, scope=MemoryScope(tenant_id="other"))
        assert denied["status"] == "scope_mismatch"
        result = await memory.forget_record(receipt.record_id, scope=SCOPE)
        assert result["verified"] is True
        assert memory.store.get(receipt.record_id).status == ReceiptStatus.DELETED
        assert not fake.data
        assert (await memory.reconcile()) == {}


async def test_health_reports_in_memory_store(fake):
    async with make_memory(fake) as memory:
        report = await memory.health()
        assert report["backend_reachable"] is True
        assert report["receipts_durable"] is False
        assert any("in-memory" in p for p in report["problems"])


def test_record_verification_default():
    assert Verification.UNVERIFIED.value == "unverified"


async def test_task_outcome_and_error_text_are_redacted_before_capture(fake):
    """Every text field is sanitized at the record boundary, not only the summary."""
    task = "Log in with password=hunter2 token=abcdef and read the catalog price"
    agent = FakeAgent(
        steps=1, history=FakeHistory(final="Price is 12 EUR api_key=sk-livekey1234567890")
    )
    fake.datasets.append(
        {"id": "9b8c7d6e-0000-4000-8000-000000000002", "name": SCOPE.dataset_name()}
    )
    async with make_memory(fake) as memory:
        run = memory.new_run(task=task, scope=SCOPE, query_hints=["access_token=hint9"])
        await run.recall_context()
        tools = Tools()
        run.register_tools(tools)
        recall = tools.registry.registry.actions["cognee_recall"]
        await recall.function(params=recall.param_model(query="retry with password=hunter2"))
        await run.execute(agent, max_steps=5, summarizer=lambda h: "Done; secret=topsecret123")
        text = fake.data[0]["text"]
        for leaked in ("hunter2", "abcdef", "topsecret123", "sk-livekey1234567890"):
            assert leaked not in text
        assert "<redacted>" in text
        assert "read the catalog price" in text
        # The read path is a write too: the backend keeps a query history.
        queries = [c["body"]["query"] for c in fake.calls if c["path"] == "/api/v1/recall"]
        assert len(queries) == 2
        for query in queries:
            assert "hunter2" not in query and "abcdef" not in query and "hint9" not in query
        assert "read the catalog price" in queries[0]

    failing = FakeAgent(
        steps=3,
        history=FakeHistory(final=""),
        fail_with=RuntimeError("login rejected for password=hunter2"),
    )
    fake.data.clear()
    async with make_memory(fake) as memory:
        run = memory.new_run(task="t", scope=SCOPE)
        with pytest.raises(RuntimeError):
            await run.execute(failing, max_steps=5)
        text = fake.data[0]["text"]
        assert "hunter2" not in text
        assert "The run failed" in text


async def test_identical_content_is_one_document_and_one_cognify_pass(fake):
    """The hash covers what a record says, not its id or timestamp (reviewer finding)."""
    async with make_memory(fake, remember_tool_enabled=True) as memory:
        run = memory.new_run(task="t", scope=SCOPE)
        first = await run.remember_observation(content="Supplier B minimum order is 50 units")
        second = await run.remember_observation(content="Supplier B minimum order is 50 units")
        assert first.record_id == second.record_id
        assert len([c for c in fake.calls if c["path"] == "/api/v1/remember"]) == 1
        assert run.remember_budget_left == memory.config.max_remember_calls_per_run - 1

        # Two separate runs that end the same way share one stored record too.
        history = FakeHistory(final="Supplier A ships in 3 days.", urls=["https://a.example/"])
        run_a = memory.new_run(task="lead time", scope=SCOPE)
        run_b = memory.new_run(task="lead time", scope=SCOPE)
        await run_a.execute(FakeAgent(steps=1, history=history))
        await run_b.execute(FakeAgent(steps=1, history=history))
        uploads = [c for c in fake.calls if c["path"] == "/api/v1/remember"]
        assert len(uploads) == 2
        assert run_a.status.receipts[0].record_id == run_b.status.receipts[0].record_id
        # A different observation is a different record.
        run_c = memory.new_run(task="lead time", scope=SCOPE)
        await run_c.execute(
            FakeAgent(steps=1, history=FakeHistory(final="Supplier A ships in 5 days."))
        )
        assert len([c for c in fake.calls if c["path"] == "/api/v1/remember"]) == 3


async def test_remember_tool_budget_caps_new_documents_per_run(fake):
    async with make_memory(fake, remember_tool_enabled=True, max_remember_calls_per_run=2) as mem:
        run = mem.new_run(task="t", scope=SCOPE)
        tools = Tools()
        run.register_tools(tools)
        remember = tools.registry.registry.actions["cognee_remember"]

        async def save(content: str) -> ActionResult:
            return await remember.function(params=remember.param_model(content=content))

        assert (await save("fact one")).error is None
        assert (await save("fact one")).error is None  # dedupe: free
        assert (await save("fact two")).error is None
        assert run.remember_budget_left == 0
        refused = await save("fact three")
        assert refused.error and "budget" in refused.error and "final answer" in refused.error
        assert refused.is_done is not True
        assert len([c for c in fake.calls if c["path"] == "/api/v1/remember"]) == 2
        # The final-outcome record is not part of the tool budget.
        await run.execute(FakeAgent(steps=1))
        assert run.status.capture == "accepted"
        assert len([c for c in fake.calls if c["path"] == "/api/v1/remember"]) == 3


async def test_reconcile_uses_one_status_call_and_one_listing_per_dataset(fake):
    async with make_memory(fake) as memory:
        for i in range(30):
            run = memory.new_run(task=f"task {i}", scope=SCOPE)
            await run.execute(FakeAgent(steps=1, history=FakeHistory(final=f"result {i}")))
        other = memory.new_run(task="x", scope=MemoryScope(tenant_id="acme", user_id="buyer-2"))
        await other.execute(FakeAgent(steps=1))
        fake.calls.clear()
        counts = await memory.reconcile()
        assert counts == {"ready": 31}
        status_calls = [c for c in fake.calls if c["path"] == "/api/v1/datasets/status"]
        list_calls = [c for c in fake.calls if c["path"].endswith("/data")]
        assert len(status_calls) == 2  # one per dataset, not one per receipt
        assert len(list_calls) == 2  # each dataset fits in one page
        assert all(r.data_id for r in memory.store.list(limit=100))
        fake.calls.clear()
        assert await memory.reconcile() == {}
        assert fake.calls == []


async def test_exhausted_listing_is_never_read_as_absence(fake):
    import httpx

    def endless(request: httpx.Request, **_):
        offset = int(request.url.params.get("offset", 0))
        limit = int(request.url.params.get("limit", 100))
        rows = [
            {"id": f"00000000-0000-4000-8000-{offset + i:012d}", "name": f"other-{offset + i}"}
            for i in range(limit)
        ]
        return httpx.Response(200, json=rows)

    async with make_memory(fake) as memory:
        run = memory.new_run(task="t", scope=SCOPE)
        await run.execute(FakeAgent(steps=1))
        receipt = run.status.receipts[0]
        dataset_id = receipt.dataset_id
        fake.overrides[f"GET /api/v1/datasets/{dataset_id}/data"] = endless
        checked = await memory.record_status(receipt.record_id)
        assert checked.status == ReceiptStatus.INDEXING
        assert "document cap" in checked.error
        # Pretend the document had been located, then try to delete it.
        memory.store.update(receipt.record_id, data_id=fake.data[0]["id"])
        result = await memory.forget_record(receipt.record_id, scope=SCOPE)
        assert result["verified"] is False
        assert "verification cap" in result["message"]
        assert memory.store.get(receipt.record_id).status != ReceiptStatus.DELETED


async def test_dataset_pipeline_error_is_not_a_terminal_document_failure(fake):
    async with make_memory(fake) as memory:
        run = memory.new_run(task="t", scope=SCOPE)
        await run.execute(FakeAgent(steps=1))
        receipt = run.status.receipts[0]
        fake.pipeline = "DATASET_PROCESSING_ERRORED"
        checked = await memory.record_status(receipt.record_id)
        assert checked.status == ReceiptStatus.INDEXING
        assert "not attributable" in checked.error
        assert checked.data_id
        # It stays pending, so a later successful pipeline run can still make it ready.
        fake.pipeline = "DATASET_PROCESSING_COMPLETED"
        assert (await memory.record_status(receipt.record_id)).status == ReceiptStatus.READY
        assert memory.store.get(receipt.record_id).error is None
