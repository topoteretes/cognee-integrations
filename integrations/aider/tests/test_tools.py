"""Tests for the Aider memory tools.

Cognee itself is mocked so the suite runs offline and for free (no LLM/API
keys), but the assertions cover the real contract: that ``add`` builds the
graph and that both tools stay scoped to the session's dataset — the two
things a memory integration must get right to recall anything and to keep
projects isolated.
"""

from unittest.mock import AsyncMock, patch

import pytest
from cognee.modules.data.exceptions import DatasetNotFoundError
from cognee_integration_aider import (
    add_project_memory,
    get_sessionized_cognee_tools,
    search_project_memory,
    session_dataset,
)


def test_session_dataset_sanitizes_and_isolates():
    ds = session_dataset("my-project/src!")
    # readable slug keeps only safe characters ...
    assert ds.startswith("aider_session_my-projectsrc")
    assert "/" not in ds and "!" not in ds
    # ... and the hash suffix keeps ids that strip to the same slug distinct
    assert session_dataset("a/b") != session_dataset("ab")
    assert session_dataset("proj_a") != session_dataset("proj_b")


@pytest.mark.asyncio
async def test_add_builds_graph_scoped_to_session():
    dataset = session_dataset("alpha")
    with (
        patch("cognee.add", new_callable=AsyncMock) as add,
        patch("cognee.cognify", new_callable=AsyncMock) as cognify,
    ):
        result = await add_project_memory("alpha", "we use postgres")

    add.assert_awaited_once_with("we use postgres", dataset_name=dataset)
    # the graph MUST be built (scoped to the same dataset) or search finds nothing
    cognify.assert_awaited_once_with(datasets=[dataset])
    assert "alpha" in result


@pytest.mark.asyncio
async def test_search_is_scoped_to_the_session():
    dataset = session_dataset("alpha")
    with patch("cognee.search", new_callable=AsyncMock) as search:
        search.return_value = ["postgres on 5432"]
        result = await search_project_memory("alpha", "which db?")

    # search MUST pass the session's dataset, otherwise memory leaks across projects
    search.assert_awaited_once_with("which db?", datasets=[dataset])
    assert "postgres on 5432" in result


@pytest.mark.asyncio
async def test_search_reports_when_empty():
    with patch("cognee.search", new_callable=AsyncMock) as search:
        search.return_value = []
        assert await search_project_memory("alpha", "anything") == "No memories found."


@pytest.mark.asyncio
async def test_search_on_unwritten_session_is_graceful():
    # cognee raises DatasetNotFoundError when recalling a session with no data yet;
    # the tool must degrade to the documented empty result, not surface the error.
    with patch("cognee.search", new_callable=AsyncMock) as search:
        search.side_effect = DatasetNotFoundError(message="No datasets found.")
        assert await search_project_memory("fresh", "anything") == "No memories found."


@pytest.mark.asyncio
async def test_sessionized_tools_bind_the_session():
    add, search = get_sessionized_cognee_tools("beta")
    with patch("cognee.search", new_callable=AsyncMock) as search_mock:
        search_mock.return_value = []
        await search("q")
    search_mock.assert_awaited_once_with("q", datasets=[session_dataset("beta")])
