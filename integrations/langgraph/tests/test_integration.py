import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from cognee_integration_langgraph import get_sessionized_cognee_tools
from cognee_integration_langgraph.tools import add_tool, search_tool


class TestLangGraphIntegration:
    """Test suite for Cognee LangGraph integration"""

    def test_get_sessionized_tools_with_session_id(self):
        """Test that get_sessionized_cognee_tools returns tools with a specific session ID"""
        session_id = "test-session-123"
        tools = get_sessionized_cognee_tools(session_id=session_id)

        assert len(tools) == 2
        assert all(hasattr(tool, "name") for tool in tools)
        tool_names = [tool.name for tool in tools]
        assert "add_tool" in tool_names
        assert "search_tool" in tool_names

    def test_get_sessionized_tools_without_session_id(self):
        """Test that get_sessionized_cognee_tools auto-generates a session ID"""
        tools = get_sessionized_cognee_tools()

        assert len(tools) == 2
        assert all(hasattr(tool, "name") for tool in tools)

    def test_get_sessionized_tools_with_persist_tool(self):
        """Test that get_sessionized_cognee_tools includes persist tool when requested"""
        session_id = "test-session-123"
        tools = get_sessionized_cognee_tools(session_id=session_id, include_persist_tool=True)

        assert len(tools) == 3
        tool_names = [tool.name for tool in tools]
        assert "add_tool" in tool_names
        assert "search_tool" in tool_names
        assert "persist_sessions_tool" in tool_names

    @pytest.mark.asyncio
    async def test_add_tool_basic(self):
        """Test that add_tool can be called with basic parameters"""
        with (
            patch(
                "cognee_integration_langgraph.tools.cognee.add", new_callable=AsyncMock
            ) as mock_add,
            patch(
                "cognee_integration_langgraph.tools.cognee.cognify",
                new_callable=AsyncMock,
            ) as mock_cognify,
        ):
            # Give time for queue processing
            result = await add_tool.ainvoke({"data": "test data"})

            # Wait for queue to be processed
            await asyncio.sleep(2.5)

            assert result == "Item added to cognee and processed"
            mock_add.assert_called()
            mock_cognify.assert_called()

    @pytest.mark.asyncio
    async def test_search_tool_basic(self):
        """Test that search_tool can be called with basic parameters"""
        with patch(
            "cognee_integration_langgraph.tools.cognee.search", new_callable=AsyncMock
        ) as mock_search:
            mock_search.return_value = ["result1", "result2"]

            result = await search_tool(query_text="test query")

            assert result == ["result1", "result2"]
            mock_search.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_tool_with_session_id(self):
        """Test that search_tool properly passes session_id"""
        with patch(
            "cognee_integration_langgraph.tools.cognee.search", new_callable=AsyncMock
        ) as mock_search:
            mock_search.return_value = ["result1"]

            await search_tool(
                query_text="test query",
                session_id="test-session-123",
            )

            # Verify session_id was passed to the search call
            call_kwargs = mock_search.call_args.kwargs
            assert call_kwargs["session_id"] == "test-session-123"

    @pytest.mark.asyncio
    async def test_sessionized_tools_integration(self):
        """Test that sessionized tools properly inject session_id"""
        with patch(
            "cognee_integration_langgraph.tools.cognee.search", new_callable=AsyncMock
        ) as mock_search:
            mock_search.return_value = ["result1"]

            session_id = "integration-test-session"
            _, search_tool_sessionized = get_sessionized_cognee_tools(session_id=session_id)

            # Test search tool with sessionization
            await search_tool_sessionized.ainvoke({"query_text": "test query"})

            # Verify session_id was automatically injected
            call_kwargs = mock_search.call_args.kwargs
            assert call_kwargs["session_id"] == session_id
            assert call_kwargs["query_text"] == "test query"

    def test_tools_have_correct_metadata(self):
        """Test that tools have proper metadata (name, description)"""
        tools = get_sessionized_cognee_tools("test-session")

        for tool in tools:
            assert hasattr(tool, "name")
            assert hasattr(tool, "description")
            assert tool.name is not None
            assert tool.description is not None
