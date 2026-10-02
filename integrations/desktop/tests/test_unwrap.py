"""cognee >=1.2 wraps search results in per-dataset envelopes; older versions
return flat lists. The adapters must feed the rest of the backend flat items
either way (see the recorded 1.4.0 shapes in unwrap_results's docstring)."""

from desktop_backend.adapters import _first_text, extract_file_hint, unwrap_results


def test_unwrap_dataset_envelopes():
    wrapped = [
        {
            "dataset_id": "ddbaa7f1",
            "dataset_name": "main",
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
    wrapped = [{"dataset_name": "main", "search_result": ["The owner is Vasilije."]}]
    assert _first_text(unwrap_results(wrapped)) == "The owner is Vasilije."
