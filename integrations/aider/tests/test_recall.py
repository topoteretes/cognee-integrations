import pytest
from unittest.mock import patch, AsyncMock
from cognee_integration_aider.tools import add_project_memory, search_project_memory


@pytest.mark.asyncio
async def test_add_project_memory_execution():
    """Test that add_project_memory calls cognee.add with correct arguments."""
    with patch("cognee.add", new_callable=AsyncMock) as mock_add:
        result = await add_project_memory("test_session", "test content")
        mock_add.assert_called_once_with("test content", dataset_name="session_test_session")
        assert "Memory added" in result


@pytest.mark.asyncio
async def test_search_project_memory_empty():
    """Test that search_project_memory returns 'No memories found' when empty."""
    with patch("cognee.search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = []
        result = await search_project_memory("test_session", "some query")
        assert result == "No memories found."


@pytest.mark.asyncio
async def test_search_project_memory_with_results():
    """Test that search_project_memory formats results correctly."""
    mock_results = ["result1", "result2"]
    with patch("cognee.search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = mock_results
        result = await search_project_memory("test_session", "some query")
        assert result == "result1\nresult2"
