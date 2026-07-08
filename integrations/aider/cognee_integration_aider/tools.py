"""Cognee memory tools shaped for Aider terminal workflows."""

from __future__ import annotations

from typing import Any

from .config import AiderCogneeConfig, load_config
from .session import build_session_id


def _cognee():
    import cognee

    return cognee


def _render(item: Any) -> str | None:
    source = getattr(item, "source", None)
    if source == "graph":
        return getattr(item, "text", None)
    if source == "session":
        return getattr(item, "answer", None) or getattr(item, "question", None)
    if source == "graph_context":
        return getattr(item, "content", None)
    if source == "trace":
        return getattr(item, "memory_context", None)
    return str(item) if item is not None else None


def render_results(results: Any) -> list[str]:
    return [text for item in (results or []) if (text := _render(item))]


def remember_kwargs(
    config: AiderCogneeConfig | None = None,
    *,
    session_id: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    cfg = config or load_config(cwd)
    return {
        "session_id": build_session_id(cfg, cwd=cwd, session_id=session_id),
        "dataset_name": cfg.dataset,
        "self_improvement": cfg.self_improvement,
    }


def search_kwargs(
    config: AiderCogneeConfig | None = None,
    *,
    session_id: str | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    cfg = config or load_config(cwd)
    return {
        "session_id": build_session_id(cfg, cwd=cwd, session_id=session_id),
        "dataset_name": cfg.dataset,
        "top_k": cfg.top_k,
    }


async def cognee_remember(
    data: str,
    *,
    config: AiderCogneeConfig | None = None,
    session_id: str | None = None,
    cwd: str | None = None,
) -> str:
    kwargs = remember_kwargs(config, session_id=session_id, cwd=cwd)
    await _cognee().remember(data, **kwargs)
    return "Item stored in Cognee memory"


async def cognee_search(
    query_text: str,
    *,
    config: AiderCogneeConfig | None = None,
    session_id: str | None = None,
    cwd: str | None = None,
) -> list[str]:
    kwargs = search_kwargs(config, session_id=session_id, cwd=cwd)
    results = await _cognee().recall(query_text, **kwargs)
    return render_results(results)


def cognee_tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "cognee_remember",
            "description": "Store durable project memory in Cognee for later Aider sessions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data": {
                        "type": "string",
                        "description": "Project fact, decision, or developer intent to remember.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional session suffix. Defaults to the project session.",
                    },
                },
                "required": ["data"],
            },
        },
        {
            "name": "cognee_search",
            "description": "Search Cognee memory for project context relevant to Aider.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query_text": {
                        "type": "string",
                        "description": "Natural-language memory search query.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional session suffix. Defaults to the project session.",
                    },
                },
                "required": ["query_text"],
            },
        },
    ]
