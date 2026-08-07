"""Integration tests for the cognify status poll (_plugin_common.wait_for_cognify).

Driven against the mock server's real GET /api/v1/datasets/status route, so the
polled URL, the query string, and the real HTTPError taxonomy are exercised
rather than a stubbed request function.

Confirms a background remember can be confirmed/abandoned correctly:
  * STARTED -> COMPLETED is reported as "completed"
  * ERRORED / deadline are distinguished (so the bridge can retry, not mark)
  * a 404 (older server without the status route) returns "unknown" immediately
  * a transient poll failure does not abort the whole deadline

claude-code only: codex's _plugin_common has no wait_for_cognify (its writes are
submit-only). Migrated from claude-code/tests/test_cognify_poll.py.
"""

from __future__ import annotations

import pytest
from utils.mock_cognee import STATUS_COMPLETED, STATUS_ERRORED, STATUS_PROCESSING

STATUS = "/api/v1/datasets/status"


@pytest.fixture
def pc(suite, isolated_modules, mock_server, monkeypatch):
    if not suite.has_background_remember:
        pytest.skip(f"{suite.name}: no wait_for_cognify (writes are submit-only)")
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


def test_poll_started_then_completed(pc, mock_server):
    mock_server.set_dataset_status([STATUS_PROCESSING, STATUS_PROCESSING, STATUS_COMPLETED])
    outcome = pc.wait_for_cognify("d1", deadline_seconds=5.0, interval_seconds=0.01)
    assert outcome == "completed"
    call = mock_server.assert_called("GET", STATUS)
    assert call["query"]["dataset"] == "d1"
    assert call["query"]["pipeline"] == "cognify_pipeline"


def test_poll_errored(pc, mock_server):
    mock_server.set_dataset_status(STATUS_ERRORED)
    assert pc.wait_for_cognify("d1", deadline_seconds=5.0, interval_seconds=0.01) == "errored"


def test_poll_timeout(pc, mock_server):
    mock_server.set_dataset_status(STATUS_PROCESSING)  # never completes
    assert pc.wait_for_cognify("d1", deadline_seconds=0.05, interval_seconds=0.01) == "timeout"


def test_poll_404_unknown(pc, mock_server):
    """An older server without the status route: give up immediately, don't loop."""
    mock_server.force_response("GET", STATUS, 404, {"detail": "Not Found"})
    assert pc.wait_for_cognify("d1", deadline_seconds=5.0, interval_seconds=0.01) == "unknown"


def test_poll_missing_dataset_id_unknown(pc, mock_server):
    assert pc.wait_for_cognify("", deadline_seconds=5.0) == "unknown"
    mock_server.assert_not_called("GET", STATUS)  # no poll without a dataset_id


def test_poll_transient_failure_then_completed(pc, mock_server):
    """A 5xx blip mid-poll must not abort the deadline."""
    mock_server.set_dataset_status([500, STATUS_COMPLETED])
    assert pc.wait_for_cognify("d1", deadline_seconds=5.0, interval_seconds=0.01) == "completed"


def test_poll_nested_pipeline_shape(pc, mock_server):
    """Multi-pipeline responses nest {pipeline: status}; the helper must unwrap."""
    mock_server.force_response("GET", STATUS, 200, {"d1": {"cognify_pipeline": STATUS_COMPLETED}})
    assert pc.wait_for_cognify("d1", deadline_seconds=5.0, interval_seconds=0.01) == "completed"


def test_poll_memify_pipeline_is_requested(pc, mock_server):
    """The bridge polls cognify and memify separately; the pipeline is a query arg."""
    assert (
        pc.wait_for_cognify(
            "d1", deadline_seconds=5.0, interval_seconds=0.01, pipeline="memify_pipeline"
        )
        == "completed"
    )
    call = mock_server.assert_called("GET", STATUS)
    assert call["query"]["pipeline"] == "memify_pipeline"
