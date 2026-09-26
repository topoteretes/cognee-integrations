"""Deterministic, mocked tests for the web-widget chat-memory adapter + server.

These run in CI with no real LLM keys: the adapter is driven against a fake
cognee HTTP client, so the tests assert its *behavior* — session scoping,
opt-out, citation parsing, graph-over-session preference, and per-conversation
forget — without touching a provider, a cognee server, or the network.

Crucially, ``recall`` returns cognee's **real** shape: the answer text with an
appended ``Evidence:`` block (that is how ``include_references=True`` surfaces
sources), not a fabricated structured ``references`` list.
"""

from unittest.mock import AsyncMock

import pytest
from cognee_integration_web_widget.adapter import _EMPTY_ANSWER, ChatMemoryAdapter
from cognee_integration_web_widget.citations import (
    document_path,
    document_title,
    document_url,
    split_evidence,
)

# A graph completion exactly as recall(include_references=True) returns it:
# the answer prose followed by an appended, grounded "Evidence:" block.
GRAPH_ANSWER = "Cognee stores memory as a knowledge graph."
GRAPH_ENTRY = {
    "source": "graph",
    "dataset_name": "web:demo:docs",
    "score": 0.91,
    "text": (
        f"{GRAPH_ANSWER}\n\n"
        "Evidence:\n"
        "- chunk 1 of document guide.md (data_id: d1, chunk_id: c1): "
        '"Cognee turns raw data into a knowledge graph."'
    ),
}


# --- Pure logic (no client needed) ------------------------------------------


def test_session_id_convention(fake_client):
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="acme", visitor_id="v1", conversation_id="c1")
    assert conv.session_id == "web:acme:v1:c1"
    assert adapter.docs_dataset("acme") == "web:acme:docs"


def test_split_evidence_parses_bullets_and_strips_block():
    prose, citations = split_evidence(GRAPH_ENTRY["text"])
    # The Evidence block is stripped from the prose shown to the user.
    assert prose == GRAPH_ANSWER
    assert "Evidence:" not in prose
    # ...and each bullet becomes a citation with its document + ids.
    assert len(citations) == 1
    assert citations[0].document == "guide.md"
    assert citations[0].data_id == "d1"
    assert citations[0].chunk_id == "c1"
    assert citations[0].snippet == "Cognee turns raw data into a knowledge graph."


def test_split_evidence_without_block_yields_no_citations():
    prose, citations = split_evidence("You told me your name is Ada.")
    assert prose == "You told me your name is Ada."
    assert citations == []


# --- Adapter over the fake HTTP client --------------------------------------


async def test_answer_returns_clean_text_and_citations(fake_client):
    fake_client.recall.return_value = [GRAPH_ENTRY]
    adapter = ChatMemoryAdapter(top_k=5, client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    answer = await adapter.answer(conversation=conv, query="What is cognee?")

    # The raw Evidence block never leaks into the displayed answer.
    assert answer.text == GRAPH_ANSWER
    assert "Evidence:" not in answer.text
    assert answer.session_id == "web:demo:v1:c1"
    assert [c.document for c in answer.citations] == ["guide.md"]
    assert answer.as_dict()["answer"] == GRAPH_ANSWER

    # recall must be session-scoped, docs-scoped, ask for references, and pass top_k.
    call = fake_client.recall.call_args
    assert call.args[0] == "What is cognee?"
    assert call.kwargs["session_id"] == "web:demo:v1:c1"
    assert call.kwargs["datasets"] == ["web:demo:docs"]
    assert call.kwargs["top_k"] == 5


async def test_answer_prefers_generated_completion_over_session_turns(fake_client):
    """A prior/echoed session turn must never be shown as the answer."""
    # recall returns session entries *before* the graph completion.
    fake_client.recall.return_value = [
        {"source": "session", "answer": "user: What is cognee?"},
        {"source": "session", "answer": "A stale answer from a previous turn."},
        GRAPH_ENTRY,
    ]
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    answer = await adapter.answer(conversation=conv, query="What is cognee?")

    assert answer.text == GRAPH_ANSWER  # not the echoed question or stale turn


async def test_answer_opt_out_recalls_without_session(fake_client):
    fake_client.recall.return_value = [GRAPH_ENTRY]
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    await adapter.answer(conversation=conv, query="hi", remember=False, use_docs=False)

    call = fake_client.recall.call_args
    assert call.kwargs["session_id"] is None  # nothing is persisted
    assert call.kwargs["datasets"] is None  # docs mode off


async def test_answer_graceful_when_dataset_missing_or_empty(fake_client):
    """A never-seeded docs dataset returns a 4xx the client maps to no results;
    the widget degrades to an empty-memory answer, not a 500."""
    fake_client.recall.return_value = []  # what the client returns on a 4xx / no hits
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    answer = await adapter.answer(conversation=conv, query="What is cognee?")

    assert answer.text == _EMPTY_ANSWER
    assert answer.citations == []
    # It still tried the docs-scoped recall.
    assert fake_client.recall.call_args.kwargs["datasets"] == ["web:demo:docs"]


async def test_refusal_answer_is_never_cited(fake_client):
    """A "no information" answer carries no Evidence block, so it is never cited."""
    fake_client.recall.return_value = [
        {"source": "graph", "text": "I don't have any information about that."}
    ]
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    answer = await adapter.answer(conversation=conv, query="What is cognee?")

    assert answer.text == "I don't have any information about that."
    assert answer.citations == []


async def test_ingest_docs_remembers_each_doc_without_session(fake_client):
    adapter = ChatMemoryAdapter(client=fake_client)
    await adapter.ingest_docs(site_id="acme", documents=["one", "", "  ", "two"])

    # Blank docs are skipped; each real doc is a shared (session-less) remember.
    assert fake_client.remember.await_count == 2
    for call in fake_client.remember.call_args_list:
        assert call.kwargs["dataset_name"] == "web:acme:docs"
        assert "session_id" not in call.kwargs


async def test_forget_clears_only_this_conversation(fake_client):
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    cleared = await adapter.forget(conversation=conv)

    assert cleared is True
    assert fake_client.forget.call_args.kwargs["dataset_name"] == "web:demo:v1:c1"


# --- Server flow (thin FastAPI proxy over the adapter) ----------------------


@pytest.fixture
def web_client(fake_client):
    """A TestClient over the widget server with the HTTP client faked."""
    from cognee_integration_web_widget import server as server_mod
    from fastapi.testclient import TestClient

    fake_client.recall = AsyncMock(return_value=[GRAPH_ENTRY])
    # The server builds its adapter at import time (real HTTP client); swap it.
    server_mod.adapter.client = fake_client
    with TestClient(server_mod.app) as test_client:
        yield test_client, fake_client


def test_chat_endpoint_returns_answer_and_citations(web_client):
    test_client, _ = web_client
    resp = test_client.post(
        "/api/chat", json={"message": "What is cognee?", "conversation_id": "c1"}
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["answer"] == GRAPH_ANSWER
    assert body["session_id"] == "web:demo:anonymous:c1"
    assert [c["document"] for c in body["citations"]] == ["guide.md"]


def test_chat_forget_command_is_not_answered(web_client):
    test_client, fake = web_client
    before = fake.recall.await_count
    resp = test_client.post("/api/chat", json={"message": "/forget", "conversation_id": "c1"})
    assert resp.status_code == 200
    assert resp.json()["citations"] == []
    # A /forget clears the conversation and is NOT sent through recall.
    assert fake.forget.call_args.kwargs["dataset_name"] == "web:demo:anonymous:c1"
    assert fake.recall.await_count == before


def test_forget_endpoint_clears_conversation(web_client):
    test_client, fake = web_client
    resp = test_client.post("/api/forget", json={"conversation_id": "c1"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["cleared"] is True
    assert body["session_id"] == "web:demo:anonymous:c1"
    assert fake.forget.call_args.kwargs["dataset_name"] == "web:demo:anonymous:c1"


# --- Evidence bullets without a quoted snippet ------------------------------
#
# Cognee Cloud grounds an answer by naming the source chunks but does not quote
# them: its bullets end at the closing parenthesis. Requiring the quoted snippet
# made every Cloud bullet fail to match, and a citation list that silently comes
# back empty looks identical to an answer with no sources.

CLOUD_EVIDENCE = (
    "Cognee Cloud authenticates API-key requests with two custom headers.\n\n"
    "Evidence:\n"
    "- chunk 1 of document cognee-cloud__api-keys "
    "(data_id: 981bfccc-4a10-43bd-811b-de3e70879649, "
    "chunk_id: 7f5ee6e7-58db-5b99-b83a-b406da213f82)\n"
    "- chunk 1 of document cognee-cloud__account-and-billing "
    "(data_id: d2518b1f-84eb-4676-8c9a-747a504611a7, "
    "chunk_id: 4e937043-1e34-5682-bcb8-abb00b5847f2)"
)


def test_split_evidence_parses_cloud_bullets_that_carry_no_snippet():
    prose, citations = split_evidence(CLOUD_EVIDENCE)
    assert prose == "Cognee Cloud authenticates API-key requests with two custom headers."
    assert [c.document for c in citations] == [
        "cognee-cloud__api-keys",
        "cognee-cloud__account-and-billing",
    ]
    # The ids still resolve a citation to its source even with nothing quoted.
    assert citations[0].data_id == "981bfccc-4a10-43bd-811b-de3e70879649"
    assert citations[0].chunk_id == "7f5ee6e7-58db-5b99-b83a-b406da213f82"
    assert citations[0].snippet == ""


def test_split_evidence_still_parses_a_quoted_snippet():
    """The documented form must keep working — this is a widening, not a swap."""
    _, citations = split_evidence(
        "Answer.\n\nEvidence:\n"
        '- chunk 3 of document report.pdf (data_id: d1, chunk_id: c1): "the quoted bit"'
    )
    assert citations[0].snippet == "the quoted bit"
    assert citations[0].document == "report.pdf"
    assert (citations[0].data_id, citations[0].chunk_id) == ("d1", "c1")


def test_split_evidence_parses_a_bullet_with_neither_ids_nor_snippet():
    _, citations = split_evidence("A.\n\nEvidence:\n- chunk 2 of document guide.md")
    assert citations[0].document == "guide.md"
    assert citations[0].snippet == ""
    assert citations[0].data_id is None


def test_chat_endpoint_returns_cloud_style_citations(fake_client):
    """End to end: a Cloud-shaped recall must reach the widget as citations."""
    from cognee_integration_web_widget import server as server_mod
    from fastapi.testclient import TestClient

    fake_client.recall = AsyncMock(return_value=[{"source": "graph", "text": CLOUD_EVIDENCE}])
    server_mod.adapter.client = fake_client
    with TestClient(server_mod.app) as c:
        body = c.post("/api/chat", json={"message": "headers?", "conversation_id": "c1"}).json()
    assert [x["document"] for x in body["citations"]] == [
        "cognee-cloud__api-keys",
        "cognee-cloud__account-and-billing",
    ]
    assert "Evidence:" not in body["answer"]


# --- Dashboard gating -------------------------------------------------------
#
# The dashboard exposes corpus contents and the visitor question log, so the
# gate is the security boundary. These assert the *refusals*, not the rendering.


@pytest.fixture
def dashboard_client(fake_client, monkeypatch):
    """Server with the dashboard enabled under a known token."""
    from cognee_integration_web_widget import server as server_mod
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server_mod, "DASHBOARD_TOKEN", "s3cret")
    # The dashboard reports which cognee it is pointed at; the shared fake is a
    # bare namespace, so give it the two attributes the real client carries.
    fake_client.base_url = "https://tenant-test.aws.cognee.ai"
    fake_client.api_key = "k"
    fake_client.list_datasets = AsyncMock(return_value=[{"name": "web:demo:docs", "id": "d1"}])
    fake_client.dataset_data = AsyncMock(return_value=[{"name": "quickstart"}])
    # The real shape: one row per side of the exchange, tagged by `user`.
    fake_client.recall_history = AsyncMock(
        return_value=[
            {
                "text": "what is cognee?",
                "user": "user",
                "datasetId": "d1",
                "createdAt": "2026-01-01",
            },
            {
                "text": "Cognee is...",
                "user": "system",
                "datasetId": "d1",
                "createdAt": "2026-01-02",
            },
            {
                "text": "how do I install?",
                "user": "user",
                "datasetId": "d1",
                "createdAt": "2026-01-03",
            },
        ]
    )
    server_mod.adapter.client = fake_client
    with TestClient(server_mod.app) as c:
        yield c


def test_dashboard_404s_when_no_token_is_configured(web_client, monkeypatch):
    """Unconfigured must look like the route does not exist, not like 'locked'."""
    from cognee_integration_web_widget import server as server_mod

    test_client, _ = web_client
    monkeypatch.setattr(server_mod, "DASHBOARD_TOKEN", None)
    assert test_client.get("/dashboard").status_code == 404
    assert test_client.get("/dashboard?token=anything").status_code == 404
    assert test_client.get("/api/dashboard?token=anything").status_code == 404


@pytest.mark.parametrize("token", ["", "wrong", "s3cre", "s3crett"])
def test_dashboard_rejects_a_bad_token(dashboard_client, token):
    assert dashboard_client.get(f"/dashboard?token={token}").status_code == 401
    assert dashboard_client.get(f"/api/dashboard?token={token}").status_code == 401


def test_dashboard_without_token_is_rejected(dashboard_client):
    assert dashboard_client.get("/dashboard").status_code == 401
    assert dashboard_client.get("/api/dashboard").status_code == 401


def test_dashboard_with_the_right_token_reports_corpus_and_questions(dashboard_client):
    body = dashboard_client.get("/api/dashboard?token=s3cret").json()
    assert body["corpus"]["exists"] is True
    assert body["corpus"]["item_count"] == 1
    # Newest first, answers excluded.
    assert [q["query"] for q in body["questions"]] == ["how do I install?", "what is cognee?"]
    assert body["questions_scoped_to_dataset"] is True
    assert body["config"]["docs_dataset"] == "web:demo:docs"


def test_dashboard_flags_a_missing_dataset_rather_than_showing_it_empty(
    dashboard_client, fake_client
):
    """A dataset that is not there is the difference between 'no data' and
    'pointed at the wrong name' — the dashboard must say which."""
    fake_client.list_datasets = AsyncMock(return_value=[{"name": "something-else", "id": "d9"}])
    body = dashboard_client.get("/api/dashboard?token=s3cret").json()
    assert body["corpus"]["exists"] is False
    assert body["corpus"]["all_datasets"] == ["something-else"]


def test_dashboard_questions_exclude_answers_and_other_datasets(dashboard_client, fake_client):
    """Only the visitor's side of the exchange, and only this widget's corpus."""
    fake_client.recall_history = AsyncMock(
        return_value=[
            {"text": "asked here", "user": "user", "datasetId": "d1", "createdAt": "2026-01-01"},
            {
                "text": "asked elsewhere",
                "user": "user",
                "datasetId": "other",
                "createdAt": "2026-01-02",
            },
            {"text": "an answer", "user": "system", "datasetId": "d1", "createdAt": "2026-01-03"},
        ]
    )
    body = dashboard_client.get("/api/dashboard?token=s3cret").json()
    assert [q["query"] for q in body["questions"]] == ["asked here"]


def test_dashboard_falls_back_to_unscoped_when_no_row_carries_the_dataset(
    dashboard_client, fake_client
):
    """Older rows record no datasetId; an empty panel would read as 'no traffic'."""
    fake_client.recall_history = AsyncMock(
        return_value=[
            {"text": "legacy question", "user": "user", "datasetId": None, "createdAt": "x"}
        ]
    )
    body = dashboard_client.get("/api/dashboard?token=s3cret").json()
    assert [q["query"] for q in body["questions"]] == ["legacy question"]
    assert body["questions_scoped_to_dataset"] is False


# --- Citations link to the published page -----------------------------------
#
# Ingesting a docs tree flattens each page's path into its document name with
# the separator doubled, so the path is recoverable. Verified against the real
# corpus: every derived URL resolves 200 on the live docs site.

DOCS = "https://docs.cognee.ai"


@pytest.mark.parametrize(
    "document,expected",
    [
        ("changelog", "changelog"),
        ("setup-configuration__llm-providers", "setup-configuration/llm-providers"),
        (
            "how-to-guides__cognee-sdk__deployment__docker",
            "how-to-guides/cognee-sdk/deployment/docker",
        ),
        # An ingest that kept the extension must not produce a .mdx path.
        ("python-api__cognify.mdx", "python-api/cognify"),
    ],
)
def test_document_path_maps_ingested_names_to_page_paths(document, expected):
    assert document_path(document) == expected


def test_document_url_trims_a_trailing_slash_on_the_base():
    assert document_url("changelog", DOCS + "/") == f"{DOCS}/changelog"


@pytest.mark.parametrize(
    "document",
    ["", "   ", "has spaces in it", "../../etc/passwd", "https://evil.example/x"],
)
def test_document_path_declines_to_invent_a_link(document):
    """A wrong citation link is worse than none — it looks authoritative."""
    assert document_path(document) is None
    assert document_url(document, DOCS) is None


def test_document_url_is_none_without_a_configured_base():
    assert document_url("changelog", None) is None
    assert document_url("changelog", "") is None


def test_document_title_is_the_page_not_the_path():
    assert document_title("how-to-guides__cognee-sdk__deployment__docker") == "docker"
    assert document_title("setup-configuration__llm-providers") == "llm providers"
    assert document_title("changelog") == "changelog"


def test_split_evidence_always_carries_the_page_path():
    """The path travels even with no base: the widget resolves it against the
    site it is embedded on, so one backend serves preview and production."""
    answer = (
        "Answer.\n\nEvidence:\n"
        "- chunk 1 of document setup-configuration__llm-providers (data_id: d1, chunk_id: c1)"
    )
    _, cites = split_evidence(answer)
    assert cites[0].path == "setup-configuration/llm-providers"
    assert cites[0].title == "llm providers"
    # No absolute url unless the backend was explicitly told where docs live.
    assert cites[0].url is None


def test_split_evidence_adds_an_absolute_url_only_when_a_base_is_configured():
    answer = (
        "Answer.\n\nEvidence:\n"
        "- chunk 1 of document setup-configuration__llm-providers (data_id: d1, chunk_id: c1)"
    )
    _, cites = split_evidence(answer, DOCS)
    assert cites[0].url == f"{DOCS}/setup-configuration/llm-providers"
    assert cites[0].path == "setup-configuration/llm-providers"


def test_split_evidence_collapses_repeated_chunks_of_one_page():
    """Several chunks of a page are one source to a reader, and the widget shows
    only four citations — a duplicate would spend a slot saying nothing new."""
    _, cites = split_evidence(
        "A.\n\nEvidence:\n"
        "- chunk 1 of document setup-configuration__embedding-providers "
        "(data_id: a, chunk_id: c1)\n"
        "- chunk 2 of document setup-configuration__embedding-providers "
        "(data_id: a, chunk_id: c2)\n"
        "- chunk 1 of document python-api__config (data_id: b, chunk_id: c3)",
        DOCS,
    )
    assert [c.title for c in cites] == ["embedding providers", "config"]


def test_split_evidence_keeps_two_chunks_that_quote_different_text():
    """Collapsing is on (document, snippet), so distinct quotes both survive."""
    _, cites = split_evidence(
        "A.\n\nEvidence:\n"
        '- chunk 1 of document report.pdf (data_id: a, chunk_id: c1): "first quote"\n'
        '- chunk 2 of document report.pdf (data_id: a, chunk_id: c2): "second quote"'
    )
    assert [c.snippet for c in cites] == ["first quote", "second quote"]


# --- Interactive dashboard: sessions and delete ------------------------------


@pytest.fixture
def interactive_client(dashboard_client, fake_client):
    """Dashboard fixture plus the session/delete client methods."""
    fake_client.list_sessions = AsyncMock(
        return_value=[
            {
                "session_id": "web:demo:visitor-a:conv-1",
                "started_at": "2026-01-01",
                "last_activity_at": "2026-01-02",
                "msg_count": 2,
            },
            {
                "session_id": "web:demo:visitor-b:conv-2",
                "started_at": "2026-01-03",
                "last_activity_at": "2026-01-04",
                "msg_count": 1,
            },
            # An agent session in the same tenant - not this widget's traffic.
            {
                "session_id": "default_session_abc",
                "started_at": "2026-01-05",
                "last_activity_at": "2026-01-06",
                "msg_count": 9,
            },
        ]
    )
    fake_client.session_detail = AsyncMock(
        return_value={
            "qas": [
                {"question": "second?", "answer": "B", "time": "2026-01-02T10:00:00"},
                {"question": "first?", "answer": "A", "time": "2026-01-01T10:00:00"},
            ]
        }
    )
    fake_client.delete_data = AsyncMock(return_value=True)
    return dashboard_client, fake_client


def test_sessions_are_filtered_to_this_widget(interactive_client):
    """The key sees the whole tenant's sessions; the widget dashboard must not."""
    client, _ = interactive_client
    body = client.get("/api/dashboard/sessions?token=s3cret").json()
    ids = [s["session_id"] for s in body["sessions"]]
    assert ids == ["web:demo:visitor-b:conv-2", "web:demo:visitor-a:conv-1"]  # newest first
    assert not any("default_session" in i for i in ids)


def test_session_detail_returns_both_sides_in_order(interactive_client):
    client, _ = interactive_client
    body = client.get("/api/dashboard/sessions/web:demo:visitor-a:conv-1?token=s3cret").json()
    assert [(t["question"], t["answer"]) for t in body["turns"]] == [
        ("first?", "A"),
        ("second?", "B"),
    ]


def test_session_detail_refuses_a_session_from_another_site(interactive_client):
    """Path traversal into another agent's conversation must not be possible."""
    client, _ = interactive_client
    assert client.get("/api/dashboard/sessions/default_session_abc?token=s3cret").status_code == 404


def test_delete_targets_only_the_widget_dataset(interactive_client):
    client, fake = interactive_client
    assert client.request("DELETE", "/api/dashboard/data/item-1?token=s3cret").status_code == 200
    # d1 is the id of web:demo:docs in the dashboard fixture.
    assert fake.delete_data.await_args.kwargs == {"dataset_id": "d1", "data_id": "item-1"}


def test_delete_surfaces_a_refusal_rather_than_reporting_success(interactive_client):
    client, fake = interactive_client
    fake.delete_data = AsyncMock(return_value=False)
    assert client.request("DELETE", "/api/dashboard/data/item-1?token=s3cret").status_code == 502


@pytest.mark.parametrize(
    "path",
    [
        "/api/dashboard/sessions",
        "/api/dashboard/sessions/web:demo:visitor-a:conv-1",
    ],
)
def test_new_routes_are_gated_too(interactive_client, path):
    client, _ = interactive_client
    assert client.get(path).status_code == 401
    assert client.get(path + "?token=wrong").status_code == 401


def test_delete_is_gated_too(interactive_client):
    client, fake = interactive_client
    assert client.request("DELETE", "/api/dashboard/data/x").status_code == 401
    assert client.request("DELETE", "/api/dashboard/data/x?token=wrong").status_code == 401
    fake.delete_data.assert_not_awaited()


def test_corpus_items_carry_ids_and_timestamps(interactive_client, fake_client):
    """The dashboard acts on rows, so each needs an id and a last-synced time."""
    fake_client.dataset_data = AsyncMock(
        return_value=[
            {
                "id": "abc",
                "name": "quickstart",
                "createdAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-02-02T00:00:00Z",
                "externalMetadata": {"_cognee": {"source_uri": "file:///app/quickstart.md"}},
            }
        ]
    )
    client, _ = interactive_client
    corpus = client.get("/api/dashboard?token=s3cret").json()["corpus"]
    assert corpus["dataset_id"] == "d1"
    item = corpus["items"][0]
    assert item["id"] == "abc"
    assert item["updated"] == "2026-02-02T00:00:00Z"
    assert item["source"] == "file:///app/quickstart.md"


# --- Reingest ----------------------------------------------------------------
#
# Reingest is delete-then-add because cognee has no in-place refresh and a
# plain re-upload of identical content is deduplicated into a no-op. That makes
# the ordering load-bearing: the bytes must be in hand before anything is
# destroyed, and a failure after the delete must not report success.


@pytest.fixture
def reingest_client(interactive_client):
    client, fake = interactive_client
    fake.dataset_data = AsyncMock(
        return_value=[
            {
                "id": "item-1",
                "name": "quickstart",
                "extension": "md",
                "mimeType": "text/markdown",
                "createdAt": "2026-01-01",
                "updatedAt": "2026-01-01",
            }
        ]
    )
    fake.fetch_raw = AsyncMock(return_value=b"# Quickstart\n")
    fake.remember_bytes = AsyncMock(return_value=None)
    return client, fake


def test_reingest_deletes_then_re_adds_preserving_name_and_type(reingest_client):
    client, fake = reingest_client
    body = client.post("/api/dashboard/data/item-1/reingest?token=s3cret").json()
    assert body == {"reingested": "item-1", "name": "quickstart", "bytes": 13}
    assert fake.delete_data.await_args.kwargs == {"dataset_id": "d1", "data_id": "item-1"}
    kw = fake.remember_bytes.await_args.kwargs
    # Re-uploading as "message.txt" would make the row unidentifiable afterwards.
    assert kw["filename"] == "quickstart.md"
    assert kw["content_type"] == "text/markdown"
    assert kw["dataset_name"] == "web:demo:docs"


def test_reingest_reads_the_bytes_before_deleting_anything(reingest_client):
    """If the stored copy cannot be read, the item must survive untouched."""
    client, fake = reingest_client
    fake.fetch_raw = AsyncMock(return_value=None)
    r = client.post("/api/dashboard/data/item-1/reingest?token=s3cret")
    assert r.status_code == 502
    assert "nothing changed" in r.json()["detail"]
    fake.delete_data.assert_not_awaited()


def test_reingest_does_not_delete_when_cognee_refuses(reingest_client):
    client, fake = reingest_client
    fake.delete_data = AsyncMock(return_value=False)
    r = client.post("/api/dashboard/data/item-1/reingest?token=s3cret")
    assert r.status_code == 502
    fake.remember_bytes.assert_not_awaited()


def test_reingest_reports_data_loss_rather_than_success(reingest_client):
    """The dangerous case: deleted, then the re-add failed. Say so loudly."""
    client, fake = reingest_client
    fake.remember_bytes = AsyncMock(side_effect=RuntimeError("upstream 500"))
    r = client.post("/api/dashboard/data/item-1/reingest?token=s3cret")
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "removed but could not be re-added" in detail
    assert "no longer in the corpus" in detail


def test_reingest_refuses_an_item_outside_the_docs_dataset(reingest_client):
    client, fake = reingest_client
    r = client.post("/api/dashboard/data/not-mine/reingest?token=s3cret")
    assert r.status_code == 404
    fake.delete_data.assert_not_awaited()


def test_reingest_is_gated(reingest_client):
    client, fake = reingest_client
    assert client.post("/api/dashboard/data/item-1/reingest").status_code == 401
    assert client.post("/api/dashboard/data/item-1/reingest?token=wrong").status_code == 401
    fake.delete_data.assert_not_awaited()


# --- Graph page opens dark ----------------------------------------------------


def test_prefer_dark_drops_the_light_class_and_seeds_only_when_unset():
    """Dark on first paint, but the page's own toggle must still win later."""
    from cognee_integration_web_widget.server import _prefer_dark

    out = _prefer_dark('<!DOCTYPE html>\n<html lang="en" class="light">\n<head><title>g</title>')
    # First paint: no light class, so the dark :root variables apply.
    assert 'class="light"' not in out
    assert '<html lang="en">' in out
    # The seed is conditional - an existing choice is never overwritten.
    assert "if(!localStorage.getItem('cognee-viz-theme'))" in out
    assert "setItem('cognee-viz-theme','dark')" in out
    # ...and it runs before the page's own scripts restore the preference.
    assert out.index("cognee-viz-theme") < out.index("<title>")


def test_prefer_dark_leaves_an_unrecognised_page_alone():
    """A changed upstream template must not be mangled, only left as-is."""
    from cognee_integration_web_widget.server import _prefer_dark

    out = _prefer_dark("<html><body>no head, no class</body></html>")
    assert out == "<html><body>no head, no class</body></html>"


# --- Analytics ----------------------------------------------------------------


@pytest.fixture
def analytics_client(interactive_client, fake_client):
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()
    # The listing is restated here rather than inherited: analytics reads
    # last_activity_at to decide which conversations can hold a question in the
    # window, so a session dated last January carrying questions stamped today
    # would describe traffic cognee never produces.
    fake_client.list_sessions = AsyncMock(
        return_value=[
            {
                "session_id": "web:demo:visitor-a:conv-1",
                "started_at": f"{today}T10:00:00+00:00",
                "last_activity_at": f"{today}T12:00:00+00:00",
            },
            {
                "session_id": "web:demo:visitor-b:conv-2",
                "started_at": f"{today}T10:00:00+00:00",
                "last_activity_at": f"{today}T12:00:00+00:00",
            },
            # An agent session in the same tenant - not this widget's traffic.
            {
                "session_id": "default_session_abc",
                "started_at": f"{today}T10:00:00+00:00",
                "last_activity_at": f"{today}T12:00:00+00:00",
            },
        ]
    )
    fake_client.session_detail = AsyncMock(
        return_value={
            "qas": [
                {
                    "question": "How do I install?",
                    "answer": "Run pip install.",
                    "time": f"{today}T10:00:00+00:00",
                },
                {
                    "question": "how do i install?",
                    "answer": "Run pip install.",
                    "time": f"{today}T11:00:00+00:00",
                },
                {
                    "question": "Obscure thing?",
                    "answer": "I don't have anything in memory for that yet.",
                    "time": f"{today}T12:00:00+00:00",
                },
            ]
        }
    )
    return interactive_client[0], fake_client


def test_analytics_counts_unanswered_by_the_exact_empty_reply(analytics_client):
    """The adapter returns one fixed string when recall finds nothing; that is
    what makes 'unanswered' a fact rather than a guess."""
    client, _ = analytics_client
    t = client.get("/api/dashboard/analytics?token=s3cret").json()["totals"]
    # Two widget sessions in the fixture, three qas each.
    assert t["questions"] == 6
    assert t["unanswered"] == 2
    assert t["answered"] == 4


def test_analytics_ranks_questions_case_insensitively(analytics_client):
    """'How do I install?' and 'how do i install?' are one question."""
    client, _ = analytics_client
    top = client.get("/api/dashboard/analytics?token=s3cret").json()["top_questions"]
    assert top[0]["count"] == 4
    assert top[0]["question"].lower() == "how do i install?"


def test_analytics_never_fetches_a_conversation_outside_the_window(analytics_client):
    """The fan-out is the slow part, so a quiet conversation costs nothing.

    A question is never newer than its session's last activity, so a session
    that went quiet before the cutoff cannot hold one - fetching it anyway made
    the seven-day view cost more every month the tenant stayed in use.
    """
    client, fake = analytics_client
    stale = dict(fake.list_sessions.return_value[0])
    stale["session_id"] = "web:demo:visitor-c:conv-3"
    stale["last_activity_at"] = "2026-01-02T10:00:00+00:00"
    fake.list_sessions = AsyncMock(return_value=[*fake.list_sessions.return_value, stale])
    fake.session_detail.reset_mock()

    body = client.get("/api/dashboard/analytics?token=s3cret").json()

    assert stale["session_id"] not in [c.args[0] for c in fake.session_detail.call_args_list]
    assert body["totals"]["conversations"] == 2


def test_analytics_keeps_a_conversation_with_no_timestamp(analytics_client):
    """Undatable means unrulable-out: pay the round trip rather than under-report."""
    client, fake = analytics_client
    undated = {"session_id": "web:demo:visitor-d:conv-4"}
    fake.list_sessions = AsyncMock(return_value=[*fake.list_sessions.return_value, undated])

    body = client.get("/api/dashboard/analytics?token=s3cret").json()

    assert body["totals"]["conversations"] == 3


def test_analytics_defaults_to_one_week(analytics_client):
    """The default window is a week - the range a reader reaches for first."""
    client, _ = analytics_client
    body = client.get("/api/dashboard/analytics?token=s3cret").json()
    assert body["days"] == 7
    assert len(body["per_day"]) == 7


@pytest.mark.parametrize("days", [1, 7, 14, 30, 90])
def test_analytics_honours_the_selected_window(analytics_client, days):
    client, _ = analytics_client
    body = client.get(f"/api/dashboard/analytics?days={days}&token=s3cret").json()
    assert body["days"] == days
    assert len(body["per_day"]) == days


@pytest.mark.parametrize("days", [0, -1, 91, 500])
def test_analytics_refuses_a_window_outside_its_bounds(analytics_client, days):
    """Rejected at the edge rather than silently clamped, so a bad link is
    visible instead of quietly answering for a different period."""
    client, _ = analytics_client
    assert client.get(f"/api/dashboard/analytics?days={days}&token=s3cret").status_code == 422


def test_analytics_series_is_dense_so_quiet_days_read_as_zero(analytics_client):
    """A day with no traffic must be a zero, not a missing point."""
    client, _ = analytics_client
    body = client.get("/api/dashboard/analytics?days=7&token=s3cret").json()
    assert len(body["per_day"]) == 7
    assert [d["day"] for d in body["per_day"]] == sorted(d["day"] for d in body["per_day"])
    assert sum(d["answered"] + d["unanswered"] for d in body["per_day"]) == 6


def test_analytics_excludes_other_sites_and_is_gated(analytics_client):
    client, fake = analytics_client
    body = client.get("/api/dashboard/analytics?token=s3cret").json()
    # The fixture's third session is an agent session, not this widget's.
    assert body["totals"]["conversations"] == 2
    assert body["totals"]["visitors"] == 2
    assert client.get("/api/dashboard/analytics").status_code == 401
    assert client.get("/api/dashboard/analytics?token=wrong").status_code == 401


# --- graph-summary falls back to the last measured run -----------------------


# --- Documentation drift ------------------------------------------------------
#
# The question is whether a page has been edited since it was ingested, and the
# corpus answers it: cognee stores each upload at text_<md5>.txt, so the item
# carries the digest of the bytes it holds. Render the page as ingest would and
# compare. updatedAt cannot be used - it moves on any reprocess.


def _ingested(root, relative, docs_url=None):
    """An item as cognee would report it after ingesting ``relative``."""
    from cognee_integration_web_widget.docs_drift import content_digest
    from cognee_integration_web_widget.docs_ingest import item_name, render_for_ingest

    # The same render ingest performs, which is the whole basis of the
    # comparison: markdown transformed, everything else stored as it stands.
    text = (root / relative).read_text(encoding="utf-8")
    digest = content_digest(render_for_ingest(text, relative, docs_url))
    return {
        "id": relative,
        "name": item_name(relative),
        "rawDataLocation": f"s3://bucket/tenant/data/text_{digest}.txt",
    }


def _items(*specs):
    return [{"id": i, "name": n, "createdAt": c} for i, n, c in specs]


def test_drift_is_off_and_silent_without_a_docs_path():
    """A hosted backend cannot see the repo, so it must claim nothing."""
    from cognee_integration_web_widget.docs_drift import drift_for_items

    out = drift_for_items(_items(("a", "guide", "2026-01-01T00:00:00Z")), None)
    assert out == {"enabled": False, "states": {}, "matched": 0, "drifted": 0, "removed": 0}


def test_drift_is_off_when_the_path_does_not_exist():
    from cognee_integration_web_widget.docs_drift import drift_for_items

    out = drift_for_items(_items(("a", "guide", "2026-01-01T00:00:00Z")), "/nope/not/here")
    assert out["enabled"] is False


def test_drift_maps_names_to_nested_paths_and_compares_content(tmp_path):
    """setup-configuration__llm-providers -> setup-configuration/llm-providers.mdx"""
    from cognee_integration_web_widget.docs_drift import drift_for_items

    (tmp_path / "setup-configuration").mkdir()
    (tmp_path / "setup-configuration" / "llm-providers.mdx").write_text("original")
    (tmp_path / "quickstart.md").write_text("original")
    items = [
        _ingested(tmp_path, "setup-configuration/llm-providers.mdx"),
        _ingested(tmp_path, "quickstart.md"),
    ]

    # Nothing touched since the ingest those digests came from.
    out = drift_for_items(items, str(tmp_path))
    assert out["enabled"] is True
    assert out["matched"] == 2
    assert out["drifted"] == 0
    assert set(out["states"].values()) == {"current"}

    # Edit both pages, leaving the corpus holding the old bytes.
    (tmp_path / "setup-configuration" / "llm-providers.mdx").write_text("rewritten")
    (tmp_path / "quickstart.md").write_text("rewritten")
    out = drift_for_items(items, str(tmp_path))
    assert out["drifted"] == 2
    assert set(out["states"].values()) == {"edited"}


def test_drift_sees_an_edit_that_was_never_committed(tmp_path):
    """The comparison is against the file, not the history.

    Dates could not see this: an uncommitted edit leaves every commit date
    where it was, so the page read as current while the corpus was stale.
    """
    import subprocess

    from cognee_integration_web_widget.docs_drift import drift_for_items

    (tmp_path / "guide.mdx").write_text("as ingested")
    items = [_ingested(tmp_path, "guide.mdx")]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "guide.mdx").write_text("edited in the working tree, never committed")

    assert drift_for_items(items, str(tmp_path))["states"] == {"guide.mdx": "edited"}


def test_drift_does_not_call_a_touched_but_unchanged_page_edited(tmp_path):
    """A revert or a reformat moves the commit date and changes no content."""
    from cognee_integration_web_widget.docs_drift import drift_for_items

    (tmp_path / "guide.mdx").write_text("stable")
    items = [_ingested(tmp_path, "guide.mdx")]

    (tmp_path / "guide.mdx").write_text("briefly different")
    (tmp_path / "guide.mdx").write_text("stable")

    out = drift_for_items(items, str(tmp_path))
    assert out["drifted"] == 0
    assert out["states"] == {"guide.mdx": "current"}


def test_drift_compares_against_the_docs_url_ingest_stamps_in(tmp_path):
    """The Source line is part of the bytes, so the two must use one base URL."""
    from cognee_integration_web_widget.docs_drift import drift_for_items

    (tmp_path / "guide.mdx").write_text("page")
    items = [_ingested(tmp_path, "guide.mdx", "https://docs.example.com")]

    assert drift_for_items(items, str(tmp_path), "https://docs.example.com")["drifted"] == 0
    assert drift_for_items(items, str(tmp_path), "https://elsewhere.example.com")["drifted"] == 1


def test_drift_says_nothing_about_an_item_with_no_digest(tmp_path):
    """An unreadable storage path and an unchanged page are different states."""
    from cognee_integration_web_widget.docs_drift import drift_for_items

    (tmp_path / "guide.mdx").write_text("page")
    item = {"id": "a", "name": "guide", "rawDataLocation": "s3://bucket/legacy-name.txt"}

    out = drift_for_items([item], str(tmp_path))
    assert out["states"] == {"a": "unknown"}
    # Not comparable, so not counted as agreeing either.
    assert out["matched"] == 0
    assert out["drifted"] == 0


def test_drift_matches_a_file_that_is_not_markdown(tmp_path):
    """item_name strips .md and .mdx and nothing else, so a .py arrives with its
    extension already in the name. Only ever appending the markdown spellings
    meant no code file was matched to its source: every one read as "not from
    docs" while sitting in the docs folder."""
    from cognee_integration_web_widget.docs_drift import drift_for_items

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "guides.py").write_text("print('as ingested')")
    (tmp_path / "page.mdx").write_text("# page")
    items = [_ingested(tmp_path, "tests/guides.py"), _ingested(tmp_path, "page.mdx")]

    out = drift_for_items(items, str(tmp_path))
    assert out["matched"] == 2
    assert set(out["states"].values()) == {"current"}

    (tmp_path / "tests" / "guides.py").write_text("print('edited')")
    assert drift_for_items(items, str(tmp_path))["states"]["tests/guides.py"] == "edited"


def test_drift_tells_a_deleted_code_file_from_one_that_was_never_there(tmp_path):
    import subprocess

    from cognee_integration_web_widget.docs_drift import drift_for_items

    (tmp_path / "script.py").write_text("x = 1")
    items = [_ingested(tmp_path, "script.py")]
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "add"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "script.py").unlink()

    out = drift_for_items(items + [{"id": "seed", "name": "message"}], str(tmp_path))
    assert out["states"]["script.py"] == "removed"
    assert out["states"]["seed"] == "foreign"


def test_drift_ignores_items_with_no_matching_file(tmp_path):
    """The demo seeds have no page behind them; they must not be called stale."""
    import subprocess

    from cognee_integration_web_widget.docs_drift import drift_for_items

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    out = drift_for_items(_items(("a", "message", "2020-01-01T00:00:00Z")), str(tmp_path))
    assert out["matched"] == 0
    # Never a documentation page - labelled, not left blank.
    assert out["states"] == {"a": "foreign"}


def test_drift_works_on_a_folder_that_is_not_a_repository(tmp_path):
    """ "Ingest whatever I want" means plain folders, which have no history."""
    from cognee_integration_web_widget.docs_drift import drift_for_items

    (tmp_path / "notes.md").write_text("kept")
    (tmp_path / "changed.md").write_text("before")
    items = [_ingested(tmp_path, "notes.md"), _ingested(tmp_path, "changed.md")]
    (tmp_path / "changed.md").write_text("after")

    out = drift_for_items(items, str(tmp_path))
    assert out["enabled"] is True
    assert out["states"] == {"notes.md": "current", "changed.md": "edited"}


def test_corpus_sync_reports_unknown_when_nothing_could_be_matched():
    from cognee_integration_web_widget.server import _corpus_sync

    assert (
        _corpus_sync({"enabled": True, "matched": 0, "drifted": 0, "removed": 0})["state"]
        == "unknown"
    )
    assert (
        _corpus_sync({"enabled": False, "matched": 0, "drifted": 0, "removed": 0})["state"]
        == "unknown"
    )


def test_corpus_sync_states():
    from cognee_integration_web_widget.server import _corpus_sync

    assert (
        _corpus_sync({"enabled": True, "matched": 251, "drifted": 0, "removed": 0})["state"]
        == "synced"
    )
    stale = _corpus_sync({"enabled": True, "matched": 251, "drifted": 4, "removed": 0})
    assert stale["state"] == "stale"
    assert stale["drifted"] == 4
    # A page deleted from the docs needs attention even with nothing edited.
    gone = _corpus_sync({"enabled": True, "matched": 251, "drifted": 0, "removed": 1})
    assert gone["state"] == "stale"
    assert gone["removed"] == 1


def test_drift_separates_a_deleted_page_from_something_that_was_never_a_page(tmp_path):
    """Both used to render blank, hiding a real problem behind a harmless one."""
    import os
    import subprocess

    from cognee_integration_web_widget.docs_drift import drift_for_items

    def git(*args, when=None):
        env = {**os.environ}
        if when:
            env["GIT_COMMITTER_DATE"] = when
            env["GIT_AUTHOR_DATE"] = when
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True, env=env)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "cognee-cloud").mkdir()
    (tmp_path / "cognee-cloud" / "schema.mdx").write_text("page")
    git("add", "-A")
    git("commit", "-qm", "add", when="2026-01-01T00:00:00+0000")
    (tmp_path / "cognee-cloud" / "schema.mdx").unlink()
    git("add", "-A")
    git("commit", "-qm", "delete", when="2026-02-01T00:00:00+0000")

    out = drift_for_items(
        _items(
            ("gone", "cognee-cloud__schema", "2026-01-15T00:00:00Z"),
            ("seed", "message", "2026-01-15T00:00:00Z"),
        ),
        str(tmp_path),
    )
    # git knows the path, the file does not exist -> deleted from the docs.
    assert out["states"]["gone"] == "removed"
    assert out["removed"] == 1
    # git has never seen it -> not documentation, and not a problem.
    assert out["states"]["seed"] == "foreign"
    # "removed" is not counted as a matched, comparable page.
    assert out["matched"] == 0


# --- Ingest transform and page listing ---------------------------------------


def test_item_name_flattens_the_path_like_the_corpus_does():
    from cognee_integration_web_widget.docs_ingest import item_name

    assert item_name("cognee-cloud/ui/api-keys.mdx") == "cognee-cloud__ui__api-keys"
    assert item_name("changelog.md") == "changelog"


def test_to_document_lifts_frontmatter_and_drops_mdx_tags():
    from cognee_integration_web_widget.docs_ingest import to_document

    out = to_document(
        '---\ntitle: "API Keys"\ndescription: "Manage keys"\nicon: "key"\n---\n\n'
        "import X from '/snippets/x.mdx'\n\n"
        "<Note>Keys are secret.</Note>\n\nBody text.\n",
        "cognee-cloud/ui/api-keys.mdx",
        "https://docs.cognee.ai",
    )
    assert out.startswith("# API Keys\n\nManage keys\n")
    assert "(Source: https://docs.cognee.ai/cognee-cloud/ui/api-keys)" in out
    # The component wrapper goes; the sentence inside it stays.
    assert "<Note>" not in out and "Keys are secret." in out
    assert "import X" not in out
    assert "icon" not in out


def test_to_document_survives_a_page_with_no_frontmatter():
    from cognee_integration_web_widget.docs_ingest import to_document

    out = to_document("Just prose.\n", "guides/x.md")
    assert out.strip() == "Just prose."


# The page decides what to tick; the backend no longer walks a folder, so the
# listing and its exclusions moved to the browser along with the reading.


@pytest.mark.parametrize(
    "path,expected",
    [
        ("guide.mdx", "guide.mdx"),
        ("a/b/c.md", "a/b/c.md"),
        ("  notes.txt  ", "notes.txt"),
        ("a\\b.md", "a/b.md"),
    ],
)
def test_ingest_accepts_paths_relative_to_the_chosen_root(path, expected):
    from cognee_integration_web_widget.server import _safe_relative

    assert _safe_relative(path) == expected


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../../.ssh/id_rsa", "a/../../b.md", "", "   ", "a//b.md", "x" * 301],
)
def test_ingest_refuses_a_path_that_is_not_relative_to_it(path):
    """These never open a file - nothing server-side does any more - but they
    become item names and citation paths, so they are refused rather than
    normalised into something the operator did not choose."""
    from cognee_integration_web_widget.server import _safe_relative

    assert _safe_relative(path) is None


def test_ingest_sends_the_browsers_text_and_never_reads_a_path(dashboard_client, fake_client):
    """The whole point of the rework: this process opens nothing."""
    client = dashboard_client
    fake_client.remember_background = AsyncMock(return_value=True)

    body = client.post(
        "/api/dashboard/ingest?token=s3cret",
        json={"files": [{"path": "guides/setup.mdx", "text": "# Setup\n\nRun it."}]},
    ).json()

    assert body["queued"] == 1
    call = fake_client.remember_background.await_args
    assert call.kwargs["filename"] == "guides__setup.md"
    assert b"Run it." in call.args[0]


def test_ingest_skips_what_would_only_cost_a_cognify_run(dashboard_client, fake_client):
    """Binary, empty and oversized files are named in the response, not silently
    dropped and not sent."""
    client = dashboard_client
    fake_client.remember_background = AsyncMock(return_value=True)

    body = client.post(
        "/api/dashboard/ingest?token=s3cret",
        json={
            "files": [
                {"path": "ok.md", "text": "real"},
                {"path": "logo.bin", "text": "PNG\x00\x00"},
                {"path": "blank.md", "text": "   \n"},
                {"path": "../escape.md", "text": "nope"},
            ]
        },
    ).json()

    assert body["queued"] == 1
    assert {s["why"] for s in body["skipped"]} == {
        "looks binary",
        "empty",
        "not a usable relative path",
    }
    assert fake_client.remember_background.await_count == 1


@pytest.mark.parametrize(
    "relative,root,expected",
    [
        ("guides/deploy.mdx", "cognee-docs", "cognee-docs/guides"),
        ("a/b/c.md", "repo", "repo/a/b"),
        ("quickstart.mdx", "cognee-docs", "cognee-docs"),
        ("guides/deploy.mdx", "", "guides"),
        # A lone file picked with no folder around it still needs a tag, or it
        # is invisible to every filtered recall rather than merely ungrouped.
        ("notes.md", "", "demo"),
    ],
)
def test_node_set_is_the_folder_under_the_chosen_root(relative, root, expected):
    from cognee_integration_web_widget.server import _node_set_for

    assert _node_set_for(relative, root) == expected


def test_ingest_tags_each_folder_as_its_own_node_set(dashboard_client, fake_client):
    """A folder can be recalled on its own; cognee's recall filters on these."""
    client = dashboard_client
    fake_client.remember_background = AsyncMock(return_value=True)

    body = client.post(
        "/api/dashboard/ingest?token=s3cret",
        json={
            "root": "cognee-docs",
            "files": [
                {"path": "guides/deploy.mdx", "text": "a"},
                {"path": "guides/ladybug.md", "text": "b"},
                {"path": "cognee-cloud/ui/schema.mdx", "text": "c"},
                {"path": "quickstart.mdx", "text": "d"},
            ],
        },
    ).json()

    assert body["queued"] == 4
    assert body["node_sets"] == [
        "cognee-docs",
        "cognee-docs/cognee-cloud/ui",
        "cognee-docs/guides",
    ]
    sent = [c.kwargs["node_set"] for c in fake_client.remember_background.await_args_list]
    assert sent == [
        ["cognee-docs/guides"],
        ["cognee-docs/guides"],
        ["cognee-docs/cognee-cloud/ui"],
        ["cognee-docs"],
    ]


def test_ingest_ignores_a_root_that_is_not_usable_as_a_tag(dashboard_client, fake_client):
    """The root arrives from the page, so it is input, not instruction."""
    client = dashboard_client
    fake_client.remember_background = AsyncMock(return_value=True)

    body = client.post(
        "/api/dashboard/ingest?token=s3cret",
        json={"root": "  /  ", "files": [{"path": "guides/a.md", "text": "x"}]},
    ).json()

    assert body["node_sets"] == ["guides"]


def test_ingest_sends_uploads_concurrently_rather_than_one_after_another(
    dashboard_client, fake_client
):
    """Each upload is a round trip of about a second, so in series a folder of
    pages holds one request open for minutes."""
    import asyncio

    from cognee_integration_web_widget import server as server_mod

    client = dashboard_client
    in_flight = {"now": 0, "peak": 0}

    async def slow(*args, **kwargs):
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        await asyncio.sleep(0.02)
        in_flight["now"] -= 1
        return True

    fake_client.remember_background = AsyncMock(side_effect=slow)
    files = [{"path": f"guides/p{i}.md", "text": "x"} for i in range(12)]

    body = client.post("/api/dashboard/ingest?token=s3cret", json={"files": files}).json()

    assert body["queued"] == 12
    assert in_flight["peak"] > 1
    assert in_flight["peak"] <= server_mod.INGEST_CONCURRENCY


def test_ingest_progress_reports_the_build_that_follows_the_upload(dashboard_client, fake_client):
    """An upload returns once cognee accepts it; the cognify runs for minutes
    afterwards, and used to have nothing watching it."""
    client = dashboard_client
    fake_client.dataset_progress = AsyncMock(
        return_value={
            "status": "running",
            "progress": {"completed_items": 40, "total_items": 251, "current_stage": "extracting"},
        }
    )

    body = client.get("/api/dashboard/ingest-progress?token=s3cret").json()

    assert body["status"] == "running"
    assert (body["completed_items"], body["total_items"]) == (40, 251)
    assert body["current_stage"] == "extracting"
    assert body["item_count"] == len(fake_client.dataset_data.return_value)


def test_ingest_progress_survives_a_run_with_no_numbers_yet(dashboard_client, fake_client):
    """progress is null until the first in-flight tick, so the corpus count has
    to carry the report until the pipeline has figures of its own."""
    client = dashboard_client
    fake_client.dataset_progress = AsyncMock(return_value={"status": "running", "progress": None})

    body = client.get("/api/dashboard/ingest-progress?token=s3cret").json()

    assert body["status"] == "running"
    assert body["completed_items"] is None and body["total_items"] is None
    assert body["item_count"] == len(fake_client.dataset_data.return_value)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/topoteretes/cognee", "topoteretes/cognee"),
        ("https://github.com/topoteretes/cognee.git", "topoteretes/cognee"),
        ("https://gitlab.com/group/sub/project/", "sub/project"),
    ],
)
def test_repo_label_is_the_owner_and_name(url, expected):
    from cognee_integration_web_widget.server import repo_label

    assert repo_label(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "/Users/milenko/cognee",
        "~/cognee",
        "file:///etc",
        "git@github.com:owner/name.git",
        "https://github.com",
        "https://github.com/owner",
        "ftp://host/owner/name",
        "",
    ],
)
def test_repo_ingest_refuses_anything_but_an_http_url(dashboard_client, fake_client, url):
    """cognee resolves the spec, so a path would be read from the tenant's own
    filesystem rather than the operator's - useless, and not a primitive this
    dashboard should hand out."""
    client = dashboard_client
    fake_client.remember_repo = AsyncMock(return_value=(True, ""))

    r = client.post("/api/dashboard/ingest-repo?token=s3cret", json={"url": url})

    assert r.status_code == 400
    fake_client.remember_repo.assert_not_awaited()


def test_repo_ingest_sends_the_url_and_tags_the_code_graph(dashboard_client, fake_client):
    client = dashboard_client
    fake_client.remember_repo = AsyncMock(return_value=(True, ""))

    body = client.post(
        "/api/dashboard/ingest-repo?token=s3cret",
        json={"url": "https://github.com/topoteretes/cognee"},
    ).json()

    assert body["repository"] == "topoteretes/cognee"
    assert body["node_set"] == "code/topoteretes/cognee"
    call = fake_client.remember_repo.await_args
    assert call.args[0] == "https://github.com/topoteretes/cognee"
    assert call.kwargs["node_set"] == ["code/topoteretes/cognee"]


def test_repo_ingest_reports_why_cognee_refused(dashboard_client, fake_client):
    """A clone can fail for reasons only cognee knows - a private repo, a bad
    name - and a status code on its own does not say which."""
    client = dashboard_client
    fake_client.remember_repo = AsyncMock(return_value=(False, "repository not found"))

    r = client.post(
        "/api/dashboard/ingest-repo?token=s3cret",
        json={"url": "https://github.com/owner/nope"},
    )

    assert r.status_code == 502
    assert "repository not found" in r.json()["detail"]


def test_repo_ingest_is_gated(dashboard_client):
    assert (
        dashboard_client.post(
            "/api/dashboard/ingest-repo", json={"url": "https://github.com/a/b"}
        ).status_code
        == 401
    )


def test_progress_follows_the_pipeline_it_is_asked_for(dashboard_client, fake_client):
    """A repository is walked by code_graph_pipeline; watching the ordinary one
    shows a run that never starts."""
    client = dashboard_client
    fake_client.dataset_progress = AsyncMock(return_value={"status": "running", "progress": None})

    client.get("/api/dashboard/ingest-progress?token=s3cret&pipeline=code_graph_pipeline")
    assert fake_client.dataset_progress.await_args.args[1] == "code_graph_pipeline"

    client.get("/api/dashboard/ingest-progress?token=s3cret")
    assert fake_client.dataset_progress.await_args.args[1] == "cognify_pipeline"

    bad = client.get("/api/dashboard/ingest-progress?token=s3cret&pipeline=whatever")
    assert bad.status_code == 400


def test_ingest_progress_is_gated(dashboard_client):
    assert dashboard_client.get("/api/dashboard/ingest-progress").status_code == 401


def test_ingest_refuses_a_selection_too_large_for_one_request(dashboard_client, fake_client):
    from cognee_integration_web_widget import server as server_mod

    client = dashboard_client
    fake_client.remember_background = AsyncMock(return_value=True)

    too_many = [{"path": f"f{i}.md", "text": "x"} for i in range(server_mod.MAX_INGEST_FILES + 1)]
    assert (
        client.post("/api/dashboard/ingest?token=s3cret", json={"files": too_many}).status_code
        == 413
    )
    fake_client.remember_background.assert_not_awaited()


def test_clear_requires_the_dataset_name_typed_exactly(dashboard_client, fake_client):
    """A checkbox is too easy for something that destroys the corpus."""
    client = dashboard_client
    fake_client.forget_dataset = AsyncMock(return_value=True)

    bad = client.post("/api/dashboard/clear?token=s3cret", json={"confirm": "web:demo:doc"})
    assert bad.status_code == 400
    fake_client.forget_dataset.assert_not_awaited()

    ok = client.post("/api/dashboard/clear?token=s3cret", json={"confirm": "web:demo:docs"})
    assert ok.status_code == 200
    fake_client.forget_dataset.assert_awaited_once()


def test_clear_reports_what_it_queued_not_what_is_gone(dashboard_client, fake_client):
    """cognee drains the dataset behind the call, so a finish cannot be claimed.

    The count is read before the delete and named for what it is. Reported as
    removed, it described a corpus that was still emptying minutes later.
    """
    client = dashboard_client
    fake_client.forget_dataset = AsyncMock(return_value=True)

    body = client.post(
        "/api/dashboard/clear?token=s3cret", json={"confirm": "web:demo:docs"}
    ).json()

    assert "items_removed" not in body
    assert body["items_queued"] == len(fake_client.dataset_data.return_value)
    assert body["cleared"] == "web:demo:docs"


def test_changing_the_corpus_drops_the_cached_graph_render(dashboard_client, fake_client):
    """The render is a picture of a corpus, so a changed corpus makes it wrong.

    The cache is keyed on the dataset id, which does not change when its
    contents do, so nothing else expires it inside its fifteen minutes.
    """
    from cognee_integration_web_widget import server as server_mod

    client = dashboard_client
    fake_client.visualize_html = AsyncMock(return_value="<html>graph</html>")
    fake_client.forget_dataset = AsyncMock(return_value=True)

    first = client.get("/api/dashboard/graph-html?token=s3cret")
    assert first.headers["X-Cache"] == "miss"
    assert client.get("/api/dashboard/graph-html?token=s3cret").headers["X-Cache"] == "hit"

    client.post("/api/dashboard/clear?token=s3cret", json={"confirm": "web:demo:docs"})

    assert server_mod._viz_cache["html"] is None
    assert client.get("/api/dashboard/graph-html?token=s3cret").headers["X-Cache"] == "miss"


def test_deleting_one_source_also_drops_the_cached_render(dashboard_client, fake_client):
    """One row is enough: the picture no longer shows what is in the corpus."""
    from cognee_integration_web_widget import server as server_mod

    client = dashboard_client
    fake_client.visualize_html = AsyncMock(return_value="<html>graph</html>")
    fake_client.delete_data = AsyncMock(return_value=True)

    client.get("/api/dashboard/graph-html?token=s3cret")
    assert server_mod._viz_cache["html"] is not None

    client.delete("/api/dashboard/data/abc?token=s3cret")

    assert server_mod._viz_cache["html"] is None


def test_clear_and_ingest_are_gated(dashboard_client, fake_client):
    client = dashboard_client
    fake_client.forget_dataset = AsyncMock(return_value=True)
    assert client.post("/api/dashboard/clear", json={"confirm": "x"}).status_code == 401
    assert client.post("/api/dashboard/ingest", json={"files": []}).status_code == 401
    fake_client.forget_dataset.assert_not_awaited()
