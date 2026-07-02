import os
import pytest
from cognee_integration_aider.config import AiderMemoryConfig

def test_config_env_precedence():
    # Inject temporary environment variables
    os.environ["COGNEE_SERVICE_URL"] = "http://custom-wsl-host:9000"
    os.environ["COGNEE_MEMORY_MODE"] = "cloud"
    
    config = AiderMemoryConfig()
    
    assert config.service_url == "http://custom-wsl-host:9000"
    assert config.default_mode == "cloud"

def test_config_defaults():
    # Clean up variables to check defaults
    if "COGNEE_SERVICE_URL" in os.environ:
        del os.environ["COGNEE_SERVICE_URL"]
        
    config = AiderMemoryConfig()
    assert config.service_url == "http://localhost:8000"
