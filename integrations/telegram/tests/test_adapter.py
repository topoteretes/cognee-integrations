"""Adapter behavior against a mocked cognee HTTP client — the key-free core."""

from cognee_integration_telegram.adapter import CogneeMemoryAdapter
from cognee_integration_telegram.citations import MessageRef


def _dm(adapter, user_id=7):
    return adapter.scope_for(chat_type="private", chat_id=user_id, user_id=user_id)


async def test_ingest_immediately_remembers_with_right_scope(mock_cognee):
    adapter = CogneeMemoryAdapter()
    scope = _dm(adapter)
    await adapter.ingest(scope, MessageRef(chat_id=7, message_id=1, text="hello", author="Ada"))

    mock_cognee.remember.assert_awaited_once()
    args, kwargs = mock_cognee.remember.call_args
    assert args[0] == "Ada: hello"
    assert kwargs["dataset_name"] == "telegram_dm_7"
    # Durable ingest: no session_id (a session-only write never creates the dataset).
    assert "session_id" not in kwargs


async def test_opted_out_chat_is_not_ingested(mock_cognee):
    adapter = CogneeMemoryAdapter()
    scope = _dm(adapter)
    adapter.opt_out(scope.chat_id)

    ingested = await adapter.ingest(scope, MessageRef(chat_id=7, message_id=1, text="secret"))
    assert ingested is False
    mock_cognee.remember.assert_not_awaited()


async def test_extract_reads_recall_dict_shapes():
    # Guards against the recall result shape drifting: over HTTP each result is a
    # dict, with the renderable text under answer / text / content.
    from cognee_integration_telegram.adapter import _answer_text

    assert _answer_text({"text": "durable answer"}) == "durable answer"
    assert _answer_text({"answer": "fresh answer"}) == "fresh answer"
    assert _answer_text({"content": "context text"}) == "context text"
    assert _answer_text({}) == ""


async def test_answer_recalls_with_references_and_resolves_citations(mock_cognee, graph_result):
    # include_references appends an Evidence block quoting the retrieved source;
    # citations resolve from that grounded block back to the original message.
    grounded = (
        "The revenue report is due Friday.\n\nEvidence:\n"
        '- chunk 1 of document text_x (data_id: a): "the revenue report is due friday"'
    )
    mock_cognee.recall.return_value = [graph_result(grounded)]
    adapter = CogneeMemoryAdapter()
    scope = _dm(adapter)
    await adapter.ingest(
        scope, MessageRef(chat_id=7, message_id=11, text="the revenue report is due friday")
    )

    answer = await adapter.answer(scope, "when is the revenue report due?")

    _, kwargs = mock_cognee.recall.call_args
    assert kwargs["dataset_name"] == "telegram_dm_7"

    assert answer.text == "The revenue report is due Friday."
    assert len(answer.citations) == 1
    assert answer.citations[0].message_id == 11


async def test_refusal_answer_is_never_cited(mock_cognee, graph_result):
    # A "no information" answer carries no Evidence block; even though it shares
    # "revenue"/"report" with a stored message, the bot must not cite it — matching
    # an answer's own words against the ledger would fabricate a source.
    mock_cognee.recall.return_value = [
        graph_result("I don't have any information about the revenue report.")
    ]
    adapter = CogneeMemoryAdapter()
    scope = _dm(adapter)
    await adapter.ingest(
        scope, MessageRef(chat_id=7, message_id=11, text="the revenue report is due friday")
    )
    answer = await adapter.answer(scope, "when is the revenue report due?")
    assert answer.citations == []


async def test_forget_clears_dataset_and_drops_ledger(mock_cognee):
    adapter = CogneeMemoryAdapter()
    scope = _dm(adapter)
    await adapter.ingest(scope, MessageRef(chat_id=7, message_id=1, text="remember me"))

    await adapter.forget(scope)

    _, kwargs = mock_cognee.forget.call_args
    assert kwargs["dataset_name"] == "telegram_dm_7"
    assert adapter.ledger.refs("telegram_dm_7") == []


async def test_answer_strips_raw_evidence_block(mock_cognee, graph_result):
    # recall appends an "Evidence:" block when include_references=True; the bot must
    # show only the answer and render its own clean sources instead.
    raw = 'The Q3 review is Friday.\n\nEvidence:\n- chunk 1 of document text_x (data_id: a): "x"'
    mock_cognee.recall.return_value = [graph_result(raw)]
    adapter = CogneeMemoryAdapter()
    answer = await adapter.answer(_dm(adapter), "when is the review?")
    assert answer.text == "The Q3 review is Friday."
    assert "Evidence:" not in answer.text


async def test_empty_recall_yields_no_citations(mock_cognee):
    # Nothing stored yet, or a chat whose dataset doesn't exist: the client maps a
    # missing dataset (4xx) to no results, so the bot says "nothing here yet".
    mock_cognee.recall.return_value = []
    adapter = CogneeMemoryAdapter()
    scope = _dm(adapter)
    answer = await adapter.answer(scope, "nothing stored yet")
    assert answer.text == ""
    assert answer.citations == []


async def test_capture_is_on_by_default_until_opt_out(mock_cognee):
    # Opt-out model: a fresh chat captures until it runs /optout.
    adapter = CogneeMemoryAdapter()
    scope = _dm(adapter)
    assert adapter.is_opted_out(scope.chat_id) is False
    await adapter.ingest(scope, MessageRef(chat_id=7, message_id=1, text="captured"))
    mock_cognee.remember.assert_awaited_once()

    adapter.opt_out(scope.chat_id)
    assert await adapter.ingest(scope, MessageRef(chat_id=7, message_id=2, text="ignored")) is False
    mock_cognee.remember.assert_awaited_once()  # unchanged
