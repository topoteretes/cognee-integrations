"""A fresh dataset's graph scope answers 404 — that is not a recall error (SDK-469, part 2).

Until the first cognify lands, the server has no graph for the dataset and
answers the graph scope with 404. On a fresh install that is every prompt of the
first session, and it used to log ``recall_error {verdict: unknown}`` each time —
pure noise that also fed the health accounting. The hook now records it as
``recall_graph_not_built`` and leaves ``recall_error`` for real failures.

The mock's forced 404 applies to every scope, which is what makes the assertion
sharp: the graph scope must be the only one *not* reported as an error.
"""

from __future__ import annotations

import json

from utils.suites import state_dir


def _events(suite, home):
    log = state_dir(suite, home) / "hook.log"
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        out.append((entry.get("event"), entry.get("detail") or {}))
    return out


def test_graph_404_is_not_a_recall_error(
    suite, run_hook, mock_server, payloads, temp_home, assert_clean_real_home
):
    mock_server.force_response("POST", "/api/v1/recall", 404, {"detail": "DatasetNotFoundError"})
    result = run_hook(
        suite,
        "session-context-lookup.py",
        stdin=payloads.user_prompt(prompt="what did we decide about the retry policy?"),
        service_url=mock_server.url,
        # The graph scope runs last, and the hook stops dispatching scopes once its
        # per-prompt budget (default 4s) is spent. On the Windows runner every
        # request to the mock takes ~2s, so with the defaults only two scopes ran
        # and the graph scope — the one this test is about — was never attempted.
        # The budget is a production latency guard, not the behaviour under test.
        env={"COGNEE_RECALL_TIMEOUT": "30", "COGNEE_RECALL_BUDGET": "120"},
    )
    assert result.returncode == 0, result.stderr

    events = _events(suite, temp_home)
    assert not [d for e, d in events if e == "recall_budget_exceeded"], (
        "the budget must not cut the scope loop short in this test"
    )
    not_built = [d for e, d in events if e == "recall_graph_not_built"]
    errors = [d for e, d in events if e == "recall_error"]
    assert not_built and not_built[0]["scope"] == ["graph"]
    assert errors, "the other scopes still 404 in this forced setup and must still be reported"
    assert all(d["scope"] != ["graph"] for d in errors), errors
