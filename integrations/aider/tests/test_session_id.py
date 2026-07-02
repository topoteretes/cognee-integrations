import pytest
from cognee_integration_aider.config import AiderMemoryConfig

def test_session_dataset_formatting():
    config = AiderMemoryConfig()
    
    # Test text scrubbing and formatting isolation
    session_raw = "my-project/wsl_dir!"
    formatted_dataset = config.get_session_dataset(session_raw)
    
    assert "aider_session_" in formatted_dataset
    assert "!" not in formatted_dataset
    assert "/" not in formatted_dataset
