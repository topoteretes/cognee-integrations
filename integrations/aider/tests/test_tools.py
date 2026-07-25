"""Tests for the Aider memory tools.

Cognee itself is mocked so the suite runs offline and for free (no LLM/API
keys), but the assertions cover the real contract: that ``add`` builds the
graph and tags it with the session's node set, and that ``search`` scopes to
the same dataset + node name — the wiring a memory integration must get right
to recall anything and to keep projects isolated. (That node-set/node-name
scoping actually isolates is proven by the live example, which mocks can't
show.)
"""

from unittest.mock import AsyncMock, patch

import cognee
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

    # write is tagged with the session's node set (what actually scopes recall)
    add.assert_awaited_once_with("we use postgres", dataset_name=dataset, node_set=[dataset])
    # the graph MUST be built or search finds nothing
    cognify.assert_awaited_once_with(datasets=[dataset])
    assert "alpha" in result


@pytest.mark.asyncio
async def test_search_is_scoped_to_the_session():
    dataset = session_dataset("alpha")
    with patch("cognee.search", new_callable=AsyncMock) as search:
        search.return_value = [{"text": "postgres on 5432", "document_id": "d1"}]
        result = await search_project_memory("alpha", "which db?")

    # A CHUNKS search scoped by BOTH dataset and node name — completion search
    # types do not scope by node set on a shared graph and would leak across projects.
    search.assert_awaited_once_with(
        "which db?",
        query_type=cognee.SearchType.CHUNKS,
        datasets=[dataset],
        node_name=[dataset],
    )
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
    ds = session_dataset("beta")
    search_mock.assert_awaited_once_with(
        "q", query_type=cognee.SearchType.CHUNKS, datasets=[ds], node_name=[ds]
    )
