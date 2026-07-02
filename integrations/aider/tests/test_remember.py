import pytest
from unittest.mock import patch, AsyncMock
from cognee_integration_aider.tools import add_project_memory, search_project_memory


@pytest.mark.asyncio
async def test_remember_codebase_intent():
    """Test that a memory can be added and then retrieved."""
    with patch("cognee.add", new_callable=AsyncMock) as mock_add:
        await add_project_memory("project_x", "Use pytest for testing")
        mock_add.assert_called_once_with("Use pytest for testing", dataset_name="session_project_x")

    with patch("cognee.search", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = ["Use pytest for testing"]
        result = await search_project_memory("project_x", "testing framework")
        assert "Use pytest for testing" in result
