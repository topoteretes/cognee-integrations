import pytest
from unittest.mock import patch
from cognee_integration_aider.tools import add_project_memory

@patch("cognee_integration_aider.tools.asyncio.run")
def test_remember_codebase_intent(mock_run):
    """
    Validates that complex codebase documentation chunks can be pushed
    to the Cognee memory layout without failure.
    """
    mock_run.return_value = "Successfully added context to session graph: aider_session_wsl_test"
    
    # Simulating saving an active design block
    complex_context = """
    Architecture Guideline:
    - All API endpoints must be defined inside /routes/
    - Database layers must use SQLAlchemy async sessions.
    """
    
    response = add_project_memory(
        session_id="wsl_test",
        content=complex_context
    )
    
    assert "Successfully added" in response
    assert "aider_session_wsl_test" in response
    mock_run.assert_called_once()
