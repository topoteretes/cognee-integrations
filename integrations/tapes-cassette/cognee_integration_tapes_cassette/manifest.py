"""Hand-authored OpenAPI spec + ``x-tapes-cassette`` manifest (``cassette/v1alpha1``).

This document is the cassette's contract with the tapes API server: tapes
fetches it from ``GET /openapi`` (pointed at via ``tapes serve --cassettes``),
validates the ``x-tapes-cassette`` block, proxies the routes under
``/v1/cassettes/cognee/...``, and converts POST routes carrying ``x-tapes-mcp``
into MCP tools named ``cognee.<tool_name>``.

Hand-authored (rather than FastAPI's generated spec) so the contract stays
explicit; tests assert it matches the routes the app actually serves.
"""

from .config import (
    CASSETTE_NAME,
    CASSETTE_VERSION,
    DEFAULT_DATASET,
    DEFAULT_TAPES_BASE_URL,
    Config,
)

_DESCRIPTION = (
    "Cognee memory over tapes session recordings: syncs completed sessions "
    "into a cognee knowledge graph and answers questions over them."
)

_SYNC_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "state": {"type": "string", "enum": ["idle", "running", "completed", "failed"]},
        "started_at": {"type": ["string", "null"]},
        "finished_at": {"type": ["string", "null"]},
        "fetched": {"type": "integer"},
        "ingested": {"type": "integer"},
        "unchanged": {"type": "integer"},
        "skipped": {"type": "integer"},
        "error": {"type": ["string", "null"]},
        "last_synced_at": {"type": ["string", "null"]},
        "dataset": {"type": "string"},
    },
    "required": ["state", "dataset"],
}


def build_openapi_spec(config: Config) -> dict:
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Cognee Cassette",
            "description": _DESCRIPTION,
            "version": CASSETTE_VERSION,
        },
        "paths": {
            "/api/sync": {
                "post": {
                    "operationId": "syncSessions",
                    "summary": "Sync tapes sessions into the cognee knowledge graph",
                    "description": (
                        "Starts an incremental sync: lists sessions newer than the "
                        "checkpoint, exports each, ingests completed ones into cognee "
                        "and runs cognify. Returns immediately unless wait=true. "
                        "Idempotent: unchanged sessions are skipped via content hashes."
                    ),
                    "tags": [CASSETTE_NAME],
                    "x-tapes-mcp": {
                        "name": "sync_sessions",
                        "annotations": {
                            "readOnlyHint": False,
                            "idempotentHint": True,
                            "openWorldHint": True,
                        },
                    },
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "full": {
                                            "type": "boolean",
                                            "default": False,
                                            "description": (
                                                "Ignore the incremental checkpoint and "
                                                "re-scan the full session history."
                                            ),
                                        },
                                        "wait": {
                                            "type": "boolean",
                                            "default": False,
                                            "description": (
                                                "Run the sync inline and return its final "
                                                "status instead of running in the background."
                                            ),
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": (
                                "Sync status snapshot (accepted=false if already running)"
                            ),
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "accepted": {"type": "boolean"},
                                            "status": _SYNC_STATUS_SCHEMA,
                                        },
                                        "required": ["accepted", "status"],
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/sync/status": {
                "post": {
                    "operationId": "syncStatus",
                    "summary": "Report the current/last sync run",
                    "tags": [CASSETTE_NAME],
                    "x-tapes-mcp": {
                        "name": "sync_status",
                        "annotations": {
                            "readOnlyHint": True,
                            "idempotentHint": True,
                            "openWorldHint": False,
                        },
                    },
                    "requestBody": {
                        "required": False,
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "additionalProperties": False}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Sync status snapshot",
                            "content": {"application/json": {"schema": _SYNC_STATUS_SCHEMA}},
                        }
                    },
                }
            },
            "/api/search": {
                "post": {
                    "operationId": "searchMemory",
                    "summary": "Search cognee memory built from tapes sessions",
                    "tags": [CASSETTE_NAME],
                    "x-tapes-mcp": {
                        "name": "search_memory",
                        "annotations": {
                            "readOnlyHint": True,
                            "idempotentHint": True,
                            "openWorldHint": True,
                        },
                    },
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["query"],
                                    "properties": {
                                        "query": {
                                            "type": "string",
                                            "description": "Natural-language question.",
                                        },
                                        "search_type": {
                                            "type": "string",
                                            "default": "GRAPH_COMPLETION",
                                            "description": (
                                                "Cognee SearchType name, e.g. "
                                                "GRAPH_COMPLETION, CHUNKS, SUMMARIES."
                                            ),
                                        },
                                        "top_k": {
                                            "type": "integer",
                                            "default": 10,
                                            "minimum": 1,
                                        },
                                    },
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Search results",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "results": {"type": "array", "items": {}},
                                        },
                                        "required": ["results"],
                                    }
                                }
                            },
                        },
                        "400": {"description": "Invalid search_type"},
                    },
                }
            },
        },
        "x-tapes-cassette": {
            "kind": "cassette/v1alpha1",
            "cassette": {
                "name": CASSETTE_NAME,
                "version": CASSETTE_VERSION,
                "display_name": "Cognee Memory",
                "description": _DESCRIPTION,
                "license": "Apache-2.0",
                "homepage": (
                    "https://github.com/topoteretes/cognee-integrations"
                    "/tree/main/integrations/tapes-cassette"
                ),
                "image": f"cognee/tapes-cassette:{CASSETTE_VERSION}",
                "port": config.port,
            },
            "depends": {
                "core": "v1",
                "views": [],
            },
            "api": {
                "health": "/ping",
                "openapi": "/openapi",
                "prefix_path": "api",
            },
            "config": [
                {
                    "key": "tapes_base_url",
                    "type": "string",
                    "default": DEFAULT_TAPES_BASE_URL,
                    "description": "Base URL of the tapes core API (env: TAPES_BASE_URL).",
                },
                {
                    "key": "dataset_name",
                    "type": "string",
                    "default": DEFAULT_DATASET,
                    "description": "Cognee dataset for session memory (env: COGNEE_TAPES_DATASET).",
                },
            ],
        },
    }
