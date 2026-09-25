"""E2e: ``cognee-search.sh`` searching a dataset other than the active one.

The cross-dataset picker ends in ``cognee-search.sh "<q>" 10 --graph
--dataset-id <uuid>``. Session history is bound to the ACTIVE dataset, so a
foreign target must reach the server as a graph-only read addressed by UUID
with no session id — whatever scope the caller asked for. The active dataset
named by hand keeps the full scope. Runs the wrapper as a subprocess against
the mock server, for both suites.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from utils.isolation import build_env, usable_bash


@pytest.fixture(autouse=True)
def _needs_cross_dataset_search(suite):
    if not suite.has_cross_dataset_search:
        pytest.skip(f"{suite.name}: no cross-dataset search flow")


OTHER_ID = "22222222-2222-4222-8222-222222222222"


BASH = usable_bash()

pytestmark = pytest.mark.skipif(
    BASH is None, reason="requires a working POSIX bash (wrapper is not run on Windows)"
)


def run_search(suite, *args, home, service_url, api_key="k", extra=None):
    script = suite.scripts_dir / "cognee-search.sh"
    env = build_env(suite, home, service_url=service_url, api_key=api_key, extra=extra)
    return subprocess.run(
        [BASH, str(script), *args],
        env=env,
        cwd=str(home),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def _recall_body(mock_server) -> dict:
    call = mock_server.assert_called("POST", "/api/v1/recall")
    return call["json"]


def test_foreign_dataset_is_a_graph_only_read_by_uuid(
    suite, temp_home, mock_server, assert_clean_real_home
):
    mock_server.set_recall_results([{"source": "graph", "content": "from elsewhere"}])
    result = run_search(
        suite,
        "what did we decide",
        "10",
        "--graph",
        "--dataset-id",
        OTHER_ID,
        home=temp_home,
        service_url=mock_server.url,
        extra={"COGNEE_SESSION_ID": "pinned_session"},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [{"source": "graph", "content": "from elsewhere"}]
    body = _recall_body(mock_server)
    assert body["dataset_ids"] == [OTHER_ID]
    assert body["scope"] == ["graph"]
    assert "session_id" not in body and "datasets" not in body


def test_foreign_dataset_drops_the_session_id_without_a_flag(
    suite, temp_home, mock_server, assert_clean_real_home
):
    result = run_search(
        suite,
        "anything",
        "5",
        "--dataset-id",
        OTHER_ID,
        home=temp_home,
        service_url=mock_server.url,
        extra={"COGNEE_SESSION_ID": "pinned_session"},
    )
    assert result.returncode == 0, result.stderr
    body = _recall_body(mock_server)
    assert body["scope"] == ["graph"] and "session_id" not in body
    assert body["dataset_ids"] == [OTHER_ID]


def test_active_dataset_named_by_hand_keeps_the_session_id(
    suite, temp_home, mock_server, assert_clean_real_home
):
    result = run_search(
        suite,
        "anything",
        "5",
        "--dataset",
        "agent_sessions",
        home=temp_home,
        service_url=mock_server.url,
        extra={"COGNEE_SESSION_ID": "pinned_session", "COGNEE_PLUGIN_DATASET": "agent_sessions"},
    )
    assert result.returncode == 0, result.stderr
    body = _recall_body(mock_server)
    # Graph only, even for the active dataset: the session cache is never searched,
    # but the session id travels so the graph item can carry this session's history.
    assert body["scope"] == ["graph"]
    assert body["session_id"] == "pinned_session"
    assert body["datasets"] == ["agent_sessions"]


def test_foreign_dataset_never_falls_back_to_the_cli(
    suite, temp_home, closed_port_url, assert_clean_real_home
):
    """A dataset addressed by UUID resolves only on the server; with the server
    absent the wrapper reports UNREACHABLE and stops, like the code lane."""
    result = run_search(
        suite,
        "anything",
        "5",
        "--graph",
        "--dataset-id",
        OTHER_ID,
        home=temp_home,
        service_url=closed_port_url,
    )
    assert result.returncode == 1
    assert result.stdout.strip() == "UNREACHABLE"
    assert "search not run" in result.stderr
    assert "falling back to cognee-cli" not in result.stderr
