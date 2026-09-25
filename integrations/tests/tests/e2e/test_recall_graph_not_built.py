"""A fresh dataset's graph scope answers 404 — that is not a recall error (SDK-469, part 2).

Until the first cognify lands, the server has no graph for the dataset and
answers the graph scope with 404. On a fresh install that is every prompt of the
first session, and it used to log ``recall_error {verdict: unknown}`` each time —
pure noise that also fed the health accounting. The hook now records it as
``recall_graph_not_built`` and leaves ``recall_error`` for real failures.

Since the one-request memory contract of cognee 1.6.0 (SDK-741) the graph scope
is the only request a plain prompt makes, so the mock's forced 404 lands on
exactly that request: it must surface as ``recall_graph_not_built`` and nothing
at all may surface as ``recall_error``.
"""

from __future__ import annotations

from utils.hooklog import hook_events


def test_graph_404_is_not_a_recall_error(
    suite, run_hook, mock_server, payloads, temp_home, assert_clean_real_home
):
    mock_server.force_response("POST", "/api/v1/recall", 404, {"detail": "DatasetNotFoundError"})
    result = run_hook(
        suite,
        "session-context-lookup.py",
        stdin=payloads.user_prompt(prompt="what did we decide about the retry policy?"),
        service_url=mock_server.url,
        # Every scope shares the per-prompt budget as its deadline. On the Windows
        # runner a request to the mock can take seconds, and a scope that times
        # out is recorded as a slow recall_error rather than the 404 this test is
        # about. The budget is a production latency guard, not the behaviour
        # under test, so it is raised well clear of the runner's latency.
        env={"COGNEE_RECALL_BUDGET": "120"},
    )
    assert result.returncode == 0, result.stderr

    events = hook_events(suite, temp_home)
    assert not [d for e, d in events if e == "recall_budget_exceeded"], (
        "the budget must not cut the scope loop short in this test"
    )
    not_built = [d for e, d in events if e == "recall_graph_not_built"]
    errors = [d for e, d in events if e == "recall_error"]
    assert [d["scope"] for d in not_built] == [["graph"]], not_built
    assert errors == [], f"a missing graph is not a recall error: {errors}"
