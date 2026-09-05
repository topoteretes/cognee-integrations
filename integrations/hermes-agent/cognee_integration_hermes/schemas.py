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
                    "RAG_COMPLETION, CHUNKS, CHUNKS_LEXICAL, TEMPORAL, or FEELING_LUCKY. "
                    "CHUNKS returns matching stored text directly — fast, no LLM in the "
                    "loop. GRAPH_COMPLETION (the default) synthesizes an answer with an "
                    "LLM per query, which can be slow on local models; prefer CHUNKS "
                    "when a query times out or raw excerpts are enough."
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
        "Delete specific content from Cognee memory when the user asks to forget "
        "something (e.g. 'forget what we said about tennis'). Two-phase: call with "
        "action='find' and terms describing what to forget — it lists candidate "
        "documents with previews; show them to the user, then call with "
        "action='forget', the confirmed data_ids, and confirm=true to delete "
        "exactly those documents (irreversible). Dataset-wide deletion requires "
        "an explicit user request: action='forget' with everything_in_dataset "
        "plus confirm=true."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "find: list matching documents. forget: delete listed ids.",
                "enum": ["find", "forget"],
            },
            "terms": {
                "type": "string",
                "description": (
                    "For action='find': words or phrases describing the content to "
                    "forget; candidates are matched against their raw stored text."
                ),
            },
            "data_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "For action='forget': the data ids to delete, exactly as returned "
                    "by action='find'."
                ),
            },
            "confirm": {
                "type": "boolean",
                "description": (
                    "Required true for action='forget'. Only set after the user has "
                    "seen the candidates and confirmed the deletion."
                ),
            },
            "dataset": {
                "type": "string",
                "description": "Optional dataset name. Defaults to the provider dataset.",
            },
            "everything_in_dataset": {
                "type": "boolean",
                "description": (
                    "Delete the whole dataset instead of individual documents. Only "
                    "when the user explicitly asked to clear the entire dataset."
                ),
            },
        },
        "required": ["action"],
    },
}

SWITCH_DATASET_SCHEMA = {
    "name": "cognee_switch_dataset",
    "description": (
        "Move this conversation to another Cognee dataset. action='list' shows the "
        "datasets visible to this principal, 'current' reports the active one, "
        "'switch' bridges the current session into its dataset and re-points "
        "capture, recall and the session-end improve at the target (creating it "
        "if needed), 'reset' returns to the configured default dataset."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "list, current, switch, or reset.",
                "enum": ["list", "current", "switch", "reset"],
            },
            "dataset": {
                "type": "string",
                "description": "For action='switch': the target dataset name.",
            },
            "force": {
                "type": "boolean",
                "description": (
                    "Proceed with a switch/reset even when bridging the current "
                    "session into its old dataset fails (the un-bridged session is "
                    "recorded and retried at session end)."
                ),
            },
        },
        "required": ["action"],
    },
}

CODE_SEARCH_SCHEMA = {
    "name": "cognee_code_search",
    "description": (
        "Query an indexed repository's deterministic code graph (symbols, calls, "
        "imports, endpoints) — exact answers, no LLM in the loop. Repositories are "
        "indexed with `hermes cognee index-repo <path-or-url>`. Operations: "
        "query_facts (substring fact lookup by name), explore (a symbol's "
        "neighborhood), traverse (walk relations from a seed), find_path (between "
        "two symbols), impact_analysis (what depends on a symbol), delta (what "
        "changed between indexings)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": "The structured code-graph operation to run.",
                "enum": [
                    "query_facts",
                    "explore",
                    "traverse",
                    "find_path",
                    "impact_analysis",
                    "delta",
                ],
            },
            "name": {
                "type": "string",
                "description": (
                    "The symbol / identifier the operation starts from (for "
                    "find_path, the source symbol)."
                ),
            },
            "target": {
                "type": "string",
                "description": "For find_path: the destination symbol.",
            },
            "repo": {
                "type": "string",
                "description": (
                    "Repository path/URL or code dataset name. Defaults to the repo "
                    "containing the current working directory, if indexed."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results to return. Default 10.",
            },
        },
        "required": ["operation"],
    },
}
