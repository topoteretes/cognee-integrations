"""Load environment (cognee endpoint / API key, or local config) on import.

Kept separate so both the nodes and the Agent Node tools pick up the same
config without importing it more than once.
"""

from dotenv import load_dotenv

load_dotenv()
