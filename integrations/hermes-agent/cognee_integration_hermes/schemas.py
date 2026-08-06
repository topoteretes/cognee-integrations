"""Tool schemas exposed by the Cognee memory provider."""

RECALL_SCHEMA = {
    "name": "cognee_recall",
    "description": (
        "Search Cognee session memory and the persistent knowledge graph for relevant "
        "information. Use for questions that may depend on prior conversations, stored "
        "facts, project context, or knowledge already captured by Cognee."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language query to search for.",
            },
            "scope": {
                "type": "string",
                "description": "Search scope: auto, session, or graph. Default: auto.",
                "enum": ["auto", "session", "graph"],
            },
            "search_type": {
                "type": "string",
                "description": (
                    "Optional Cognee SearchType override, for example GRAPH_COMPLETION, "
                    "RAG_COMPLETION, CHUNKS, CHUNKS_LEXICAL, TEMPORAL, or FEELING_LUCKY."
                ),
            },
            "top_k": {
                "type": "integer",
                "description": "Maximum number of results to return. Default: provider config.",
            },
        },
        "required": ["query"],
    },
}

REMEMBER_SCHEMA = {
    "name": "cognee_remember",
    "description": (
        "Persist important content into Cognee's knowledge graph. Use when the user "
        "explicitly asks to remember, store, save, or preserve a durable fact or decision."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Text content to store permanently.",
            },
            "dataset": {
                "type": "string",
                "description": "Optional Cognee dataset name. Defaults to the provider dataset.",
            },
        },
        "required": ["content"],
    },
}

FORGET_SCHEMA = {
    "name": "cognee_forget",
    "description": (
        "Delete Cognee memory. Use only when the user asks to remove or clear stored "
        "information. Requires either a dataset or everything=true."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "dataset": {
                "type": "string",
                "description": "Dataset to delete.",
            },
            "everything": {
                "type": "boolean",
                "description": "Delete all Cognee data visible to this integration.",
            },
            "memory_only": {
                "type": "boolean",
                "description": "Delete only memory-layer records when supported by Cognee.",
            },
        },
        "required": [],
    },
}
