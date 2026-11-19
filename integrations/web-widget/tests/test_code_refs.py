"""Related code: the names picked out of a message, and what is reported back.

The messages are shaped like real docs-assistant traffic (pasted log lines and
tracebacks, questions naming a class); the code-graph results mirror what
cognee's code scope returns: a JSON ``explore`` payload with a ``focus`` node.
"""

import json
from unittest.mock import AsyncMock

import pytest
from cognee_integration_web_widget.adapter import _EMPTY_ANSWER, ChatMemoryAdapter
from cognee_integration_web_widget.code_refs import (
    CODE_CONTEXT_HEADER,
    MAX_CODE_CONTEXT_CHARS,
    code_context_block,
    extract_code_seeds,
    parse_code_entry,
)

ADAPTER_SYMBOL = (
    "cognee/infrastructure/llm/structured_output_framework/litellm_native/"
    "native_adapter.NativeLiteLLMAdapter.acreate_structured_output"
)
ADAPTER_FILE = (
    "cognee/infrastructure/llm/structured_output_framework/litellm_native/native_adapter.py"
)


def code_entry(name, kind="CodeSymbol", file=ADAPTER_FILE, line=419, calls=(), callers=()):
    focus = {
        "id": f"id-{name}",
        "name": name,
        "type": kind,
        "symbol_kind": "method" if kind == "CodeSymbol" else None,
        "file": file,
        "line": line,
        "repo": "cognee",
        "relations": [{"type": "calls", "target": t} for t in calls],
    }
    edges = [{"type": "calls", "source": c, "target_id": f"id-{name}"} for c in callers]
    payload = {"operation": "explore", "focus": focus, "edges": edges}
    return {"source": "code", "text": json.dumps(payload)}


GRAPH_ENTRY = {"source": "graph", "text": "The LLM call exceeded the model's context window."}


# --- Picking names out of a message -------------------------------------------


def test_a_log_line_yields_the_class_and_method_that_raised():
    log = (
        "2026-09-07T13:57:45 [warning] Retrying cognee.infrastructure.llm."
        "structured_output_framework.litellm_native.native_adapter.NativeLiteLLMAdapter."
        "acreate_structured_output in 16.1 seconds as it raised BadRequestError: ..."
    )
    seeds = extract_code_seeds(log)
    assert seeds[0] == "NativeLiteLLMAdapter.acreate_structured_output"
    # The class alone is covered by the Class.method seed, not looked up twice.
    assert "NativeLiteLLMAdapter" not in seeds


def test_a_traceback_path_is_cut_to_the_repository_path():
    frame = (
        'File "/home/u/.venv/lib/python3.12/site-packages/cognee/modules/search/'
        'methods/hybrid_deferral.py", line 24, in reject_hybrid_graph_only_knobs'
    )
    assert extract_code_seeds(frame) == [
        "cognee/modules/search/methods/hybrid_deferral.reject_hybrid_graph_only_knobs",
        "cognee/modules/search/methods/hybrid_deferral.py",
    ]
    # A path quoted without a frame is looked up as the file.
    path = "/home/u/.venv/lib/python3.12/site-packages/cognee/modules/search/methods/search.py"
    assert extract_code_seeds(path) == ["cognee/modules/search/methods/search.py"]


def test_a_traceback_leads_with_the_innermost_cognee_frame_and_its_error():
    # From a real docs-assistant session: the user's project folder is called
    # test_cognee, and the stack runs through asyncio before reaching cognee.
    tb = (
        "CogneeValidationError: InvalidHybridSearchConfig: wide_search_top_k requires "
        "query_type=SearchType.GRAPH_COMPLETION. (Status code: 422)\n"
        'File "/home/u/projects/test_cognee/ex_2.py", line 21, in <module>\n'
        'File "/usr/lib/python3.12/asyncio/base_events.py", line 691, in run_until_complete\n'
        'File "/home/u/projects/test_cognee/env/lib/python3.12/site-packages/cognee/api/v1/'
        'search/search.py", line 374, in search\n'
        'File "/home/u/projects/test_cognee/env/lib/python3.12/site-packages/cognee/modules/'
        'search/methods/hybrid_deferral.py", line 24, in reject_hybrid_graph_only_knobs\n'
    )
    # The function that raised, named the way the code graph names it.
    assert extract_code_seeds(tb) == [
        "cognee/modules/search/methods/hybrid_deferral.reject_hybrid_graph_only_knobs",
        "CogneeValidationError",
        "SearchType",
    ]


def test_a_constant_is_looked_up_by_its_class():
    assert extract_code_seeds("use SearchType.GRAPH_COMPLETION here") == ["SearchType"]


def test_products_and_file_names_are_not_code():
    assert extract_code_seeds("I host it on GitHub and run Activate.ps1 with OpenAI") == []


def test_a_file_outside_cognee_is_looked_up_by_its_name():
    assert extract_code_seeds("see native_adapter.py please") == ["native_adapter.py"]


def test_class_names_in_an_error_are_seeds():
    seeds = extract_code_seeds("CogneeValidationError: InvalidHybridSearchConfig: bad knob")
    assert seeds == ["CogneeValidationError", "InvalidHybridSearchConfig"]


@pytest.mark.parametrize(
    "message",
    [
        "do you provide an option for external auth providers?",
        "see https://docs.cognee.ai/guides/time-awareness for details",
        "I am only using pi / opencode",
    ],
)
def test_a_plain_question_has_nothing_to_look_up(message):
    assert extract_code_seeds(message) == []


def test_at_most_three_names_are_looked_up():
    assert len(extract_code_seeds("AlphaOne BetaTwo GammaThree DeltaFour")) == 3


# --- Reading a code-graph result ------------------------------------------------


def test_a_symbol_is_named_without_its_module_path_and_linked_to_its_line():
    ref = parse_code_entry(code_entry(ADAPTER_SYMBOL), "https://github.com/o/cognee/blob/main/")
    assert ref.name == "NativeLiteLLMAdapter.acreate_structured_output"
    assert ref.symbol == ADAPTER_SYMBOL
    assert ref.url == f"https://github.com/o/cognee/blob/main/{ADAPTER_FILE}#L419"


def test_a_file_is_named_by_its_file_name():
    ref = parse_code_entry(code_entry(ADAPTER_FILE, kind="CodeFileReference", line=1))
    assert ref.name == "native_adapter.py"
    assert ref.url is None  # no browse URL configured


@pytest.mark.parametrize(
    "entry",
    [
        {"source": "code", "text": "not json"},
        {"source": "code", "text": json.dumps({"operation": "explore", "nodes": []})},
    ],
)
def test_a_result_without_a_focus_node_reports_nothing(entry):
    assert parse_code_entry(entry) is None


# --- The adapter reports what cognee found --------------------------------------


async def test_answer_reports_the_code_named_in_the_message(fake_client):
    fake_client.recall.return_value = [GRAPH_ENTRY]
    fake_client.code_lookup = AsyncMock(return_value=[code_entry(ADAPTER_SYMBOL)])
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    answer = await adapter.answer(
        conversation=conv, query="Why does NativeLiteLLMAdapter.acreate_structured_output fail?"
    )

    assert answer.text == GRAPH_ENTRY["text"]
    assert [c.name for c in answer.code] == ["NativeLiteLLMAdapter.acreate_structured_output"]
    assert answer.as_dict()["code"][0]["file"] == ADAPTER_FILE
    # Looked up by name in the docs dataset alone, never every dataset.
    seed = fake_client.code_lookup.await_args.args[0]
    assert seed == "NativeLiteLLMAdapter.acreate_structured_output"
    assert fake_client.code_lookup.await_args.kwargs["datasets"] == ["web:demo:docs"]


async def test_a_question_without_names_makes_no_code_lookup(fake_client):
    fake_client.recall.return_value = [GRAPH_ENTRY]
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    answer = await adapter.answer(conversation=conv, query="what is an event?")

    assert answer.code == []
    fake_client.code_lookup.assert_not_awaited()


async def test_code_from_the_answer_recall_and_the_lookup_is_reported_once(fake_client):
    fake_client.recall.return_value = [GRAPH_ENTRY, code_entry(ADAPTER_SYMBOL)]
    fake_client.code_lookup = AsyncMock(return_value=[code_entry(ADAPTER_SYMBOL)])
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    answer = await adapter.answer(conversation=conv, query="NativeLiteLLMAdapter")

    assert len(answer.code) == 1


async def test_a_failing_code_lookup_never_fails_the_answer(fake_client):
    fake_client.recall.return_value = [GRAPH_ENTRY]
    fake_client.code_lookup = AsyncMock(side_effect=OSError("cognee unreachable"))
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    answer = await adapter.answer(conversation=conv, query="NativeLiteLLMAdapter")

    assert answer.text == GRAPH_ENTRY["text"]
    assert answer.code == []


async def test_code_is_reported_but_never_becomes_the_answer_text(fake_client):
    fake_client.recall.return_value = [code_entry(ADAPTER_SYMBOL)]
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    answer = await adapter.answer(conversation=conv, query="q")

    assert answer.text == _EMPTY_ANSWER
    assert [c.name for c in answer.code] == ["NativeLiteLLMAdapter.acreate_structured_output"]


def test_chat_endpoint_returns_related_code(fake_client):
    from cognee_integration_web_widget import server as server_mod
    from fastapi.testclient import TestClient

    fake_client.recall = AsyncMock(return_value=[GRAPH_ENTRY])
    fake_client.code_lookup = AsyncMock(return_value=[code_entry(ADAPTER_SYMBOL)])
    server_mod.adapter.client = fake_client
    with TestClient(server_mod.app) as client:
        body = client.post(
            "/api/chat", json={"message": "NativeLiteLLMAdapter fails", "conversation_id": "c1"}
        ).json()
    assert body["code"][0]["name"] == "NativeLiteLLMAdapter.acreate_structured_output"
    assert body["code"][0]["line"] == 419


# --- What the answering model is told -------------------------------------------


def test_the_block_names_the_code_where_it_lives_and_who_calls_it():
    entry = code_entry(
        ADAPTER_SYMBOL,
        calls=["cognee/shared/rate_limiting.llm_rate_limiter_context_manager"],
        callers=["cognee/modules/retrieval/utils/completion.generate_completion"],
    )
    block = code_context_block([entry])
    assert block.startswith(CODE_CONTEXT_HEADER)
    assert (
        f"- NativeLiteLLMAdapter.acreate_structured_output (method), defined in {ADAPTER_FILE}:419"
    ) in block
    assert "called by: generate_completion" in block
    assert "calls: llm_rate_limiter_context_manager" in block


def test_long_relation_lists_are_cut_and_counted():
    entry = code_entry(ADAPTER_SYMBOL, calls=[f"cognee/m.f{i}" for i in range(9)])
    assert "calls: f0, f1, f2, f3, f4, f5 (+3 more)" in code_context_block([entry])


def test_the_block_stays_within_its_budget():
    entries = [code_entry(f"cognee/m.Thing{i}") for i in range(200)]
    assert len(code_context_block(entries)) <= MAX_CODE_CONTEXT_CHARS


def test_no_code_means_no_block():
    assert code_context_block([]) == ""
    assert code_context_block([{"source": "code", "text": "not json"}]) == ""


async def test_the_code_found_reaches_the_answering_model(fake_client):
    fake_client.recall.return_value = [GRAPH_ENTRY]
    fake_client.code_lookup = AsyncMock(return_value=[code_entry(ADAPTER_SYMBOL)])
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    await adapter.answer(conversation=conv, query="NativeLiteLLMAdapter.acreate_structured_output")

    extra = fake_client.recall.await_args.kwargs["system_prompt_extra"]
    assert "NativeLiteLLMAdapter.acreate_structured_output" in extra
    # The question itself is untouched: search and the session see what was typed.
    assert fake_client.recall.await_args.args[0] == "NativeLiteLLMAdapter.acreate_structured_output"


async def test_a_message_without_code_adds_nothing_to_the_prompt(fake_client):
    fake_client.recall.return_value = [GRAPH_ENTRY]
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    await adapter.answer(conversation=conv, query="what is an event?")

    assert fake_client.recall.await_args.kwargs["system_prompt_extra"] is None


def test_the_block_says_what_a_function_raises_and_skips_test_callers():
    focus_id = "id-fn"
    payload = {
        "operation": "explore",
        "focus": {
            "id": focus_id,
            "name": "cognee/modules/search/methods/hybrid_deferral.reject_hybrid_graph_only_knobs",
            "type": "CodeSymbol",
            "symbol_kind": "function",
            "file": "cognee/modules/search/methods/hybrid_deferral.py",
            "line": 13,
            "relations": [
                {
                    "type": "instantiates",
                    "target": "cognee/exceptions/exceptions.CogneeValidationError",
                }
            ],
        },
        "edges": [
            {
                "type": "calls",
                "target_id": focus_id,
                "source": (
                    "cognee/modules/search/methods/get_retriever_output._effective_search_type"
                ),
            },
            {
                "type": "calls",
                "target_id": focus_id,
                "source": "cognee/tests/unit/modules/search/test_hybrid_deferral.py",
            },
        ],
    }
    block = code_context_block([{"source": "code", "text": json.dumps(payload)}])
    assert "- reject_hybrid_graph_only_knobs (function)" in block
    assert "called by: _effective_search_type\n" in block + "\n"
    assert "creates: CogneeValidationError" in block
    assert "test_hybrid_deferral" not in block


@pytest.mark.parametrize(
    ("message", "seed"),
    [
        ("how does cognee remember work?", "cognee/api/v1/remember/remember.remember"),
        ("How does cognee's recall pick results?", "cognee/api/v1/recall/recall.recall"),
        ("what does cognee.forget(dataset=...) do", "cognee/api/v1/forget/forget.forget"),
        ("should I call `improve` first?", "cognee/api/v1/improve/improve.improve"),
        ("then I call cognify() again", "cognee/api/v1/cognify/cognify.cognify"),
    ],
)
def test_a_cognee_operation_is_looked_up_where_it_is_defined(message, seed):
    assert extract_code_seeds(message) == [seed]


@pytest.mark.parametrize(
    "message",
    [
        "I remember you have prompt extract for sql guide",
        "how do I add a file?",
        "Can I search my docs?",
    ],
)
def test_the_same_words_in_plain_english_are_not_code(message):
    assert extract_code_seeds(message) == []
