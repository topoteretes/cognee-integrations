"""Cognee cassette for Paper's tapes — memory over recorded agent sessions.

Implements the tapes ``cassette/v1alpha1`` contract: an independent HTTP
service that tapes discovers via ``--cassettes``, validates against the
``x-tapes-cassette`` manifest served at ``/openapi``, and proxies under
``/v1/cassettes/cognee/...``. POST routes carry ``x-tapes-mcp`` extensions so
agents see them as MCP tools (``cognee.sync_sessions``, ``cognee.sync_status``,
``cognee.search_memory``).
"""

from .config import CASSETTE_VERSION, Config, load_config
from .manifest import build_openapi_spec
from .server import create_app

__all__ = [
    "CASSETTE_VERSION",
    "Config",
    "load_config",
    "build_openapi_spec",
    "create_app",
]
