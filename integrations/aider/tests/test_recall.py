import pytest
from unittest.mock import patch
from cognee_integration_aider.tools import add_project_memory, search_project_memory

@patch("cognee_integration_aider.tools.asyncio.run")
def test_add_project_memory_execution(mock_run):
    mock_run.return_value = "Successfully added context to session graph: aider_session_test"
    
    response = add_project_memory("test", "Setting up continuous integration rules.")
    assert "Successfully added" in response
    mock_run.assert_called_once()

@patch("cognee_integration_aider.tools.asyncio.run")
def test_search_project_memory_empty(mock_run):
    mock_run.return_value = "No matching historical context found in memory."
    
    response = search_project_memory("test", "Unknown component context")
    assert "No matching historical context" in response
