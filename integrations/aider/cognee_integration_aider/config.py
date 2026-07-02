import os

class AiderMemoryConfig:
    def __init__(self):
        # 1. Check Environment Variables, 2. Fall back to configuration defaults
        self.service_url = os.getenv("COGNEE_SERVICE_URL", "http://localhost:8000")
        self.api_key = os.getenv("COGNEE_API_KEY", None)
        self.default_mode = os.getenv("COGNEE_MEMORY_MODE", "local") # local vs cloud
        
    def get_session_dataset(self, session_id: str) -> str:
        # Resolves unique isolated graph contexts per workspace
        clean_id = "".join(c for c in session_id if c.isalnum() or c in ("_", "-"))
        return f"aider_session_{clean_id}"
