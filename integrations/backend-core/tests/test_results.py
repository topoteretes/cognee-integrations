"""Shape helpers pinned against recorded cognee 1.4.0 payloads."""

from cognee_backend_core import extract_file_hint, first_text, unwrap_results


def test_unwrap_dataset_envelopes():
    wrapped = [
        {
            "dataset_id": "ddbaa7f1",
            "dataset_name": "spotlight",
            "search_result": [
                {"text": "Carbonara: eggs...", "document_name": "pasta"},
                {"text": "Q3 roadmap...", "document_name": "quarterly-roadmap"},
            ],
        }
    ]
    flat = unwrap_results(wrapped)
    assert len(flat) == 2
    assert extract_file_hint(flat[0]) == "pasta"


def test_unwrap_passes_flat_lists_through():
    flat = [{"text": "hello"}, "plain answer"]
    assert unwrap_results(flat) == flat
    assert unwrap_results(None) == []


def test_first_text_on_unwrapped_graph_completion():
    wrapped = [{"dataset_name": "spotlight", "search_result": ["The owner is Vasilije."]}]
    assert first_text(unwrap_results(wrapped)) == "The owner is Vasilije."


def test_extract_file_hint_nested():
    chunk = {"meta": {"origin": [{"raw_data_location": "/docs/a.md"}]}}
    assert extract_file_hint(chunk) == "/docs/a.md"
    assert extract_file_hint({"no": "hints"}) is None


def test_best_text_skips_refusals_from_irrelevant_datasets():
    from cognee_backend_core import best_text

    results = [
        "I'm sorry, but the knowledge-graph you provided contains only technical entities.",
        "**Main competitors** - StayFinder, a Tier 1 direct threat with aggressive paid search.",
    ]
    assert best_text(results).startswith("**Main competitors**")
    # all refusals -> still returns something rather than nothing
    refusals = ["Sorry, no information available.", "The graph does not contain that."]
    assert best_text(refusals) in refusals
    assert best_text([]) == ""
