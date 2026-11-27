"""``cognee-search.sh --code`` searches the graph of the checkout it runs in.

The dataset comes from the checkout's index state, and only from a state built
on the server the search queries: a graph lives on the server that built it, so
another server's dataset name would find nothing there, or the wrong thing.
"""

from __future__ import annotations

import pytest

QUERIED = "https://tenant-a.cognee.example"


@pytest.mark.parametrize(
    ("server", "expected"),
    [
        (QUERIED, "codebase-proj-1234"),
        ("https://tenant-b.cognee.example", ""),
        ("http://localhost:8011", ""),
    ],
)
def test_the_code_dataset_is_the_one_on_the_server_searched(
    suite, hook_module, isolated_modules, tmp_path, server, expected
):
    if not suite.asks_before_code_index:
        pytest.skip("this host's code search does not bind graphs to a server")
    search = hook_module(suite, "cognee_search.py")
    cg = isolated_modules(suite, "_code_graph")
    repo = (tmp_path / "proj").resolve()
    (repo / "src").mkdir(parents=True)
    cg.save_repo_state(
        {
            "spec": "https://github.com/acme/proj",
            "spec_kind": "url",
            "repo_root": str(repo),
            "server": cg.server_identity(server),
            "dataset": "codebase-proj-1234",
            "fingerprint": "seed",
        }
    )
    assert search._code_dataset(str(repo / "src"), QUERIED) == expected
