"""Sessionized Cognee memory tools for the Aider CLI.

Each session (typically one software project) maps to its own Cognee dataset
and node set, so memories from different projects stay isolated on both write
and read. Writes are tagged with a per-session ``node_set``; recall uses a
``CHUNKS`` search scoped with the matching ``node_name``, which retrieves only
that session's stored snippets.

This matters because a local Cognee keeps one shared graph across datasets, and
the *completion* search types (GRAPH_COMPLETION / RAG_COMPLETION) do not scope
their retrieval by ``node_name`` — they synthesize an answer over the whole
graph and would surface another project's facts. ``CHUNKS`` + ``node_name`` is
what actually isolates one project's recall from another's (verified live).
"""

import functools
import hashlib
from typing import Callable

import cognee
from cognee.modules.data.exceptions import DatasetNotFoundError


def session_dataset(session: str) -> str:
    """Return the per-session Cognee dataset / node-set name, isolating a project.

    The name keeps a readable, sanitized slug of the session id and appends a
    short hash of the raw id, so two distinct sessions never collapse onto the
    same identifier (character-stripping alone is lossy — ``a/b`` and ``ab``
    would otherwise clash).
    """
    slug = "".join(c for c in session if c.isalnum() or c in ("_", "-"))
    digest = hashlib.sha1(session.encode("utf-8")).hexdigest()[:8]
    return f"aider_session_{slug}_{digest}"


async def add_project_memory(session: str, content: str) -> str:
    """Store ``content`` in the session's memory and build its knowledge graph.

    ``cognee.add`` only ingests raw data; ``cognee.cognify`` is what turns it
    into the graph that search reads from, so both are required for recall. The
    ``node_set`` tag is what scopes recall back to this session.
    """
    dataset = session_dataset(session)
    await cognee.add(content, dataset_name=dataset, node_set=[dataset])
    await cognee.cognify(datasets=[dataset])
    return f"Memory added to session '{session}'."


def _chunk_text(result) -> str:
    """Pull the stored snippet text out of one CHUNKS search result (dict/obj/str)."""
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        return str(result.get("text", "")).strip()
    return str(getattr(result, "text", "") or "").strip()


async def search_project_memory(session: str, query: str) -> str:
    """Retrieve memories relevant to ``query``, scoped to this session only.

    Uses a ``CHUNKS`` search filtered by the session's ``node_name`` so recall
    returns only this project's stored snippets. Completion search types do not
    scope their retrieval by node set on a shared local graph and would leak
    other projects' facts, so they are deliberately not used here.
    """
    dataset = session_dataset(session)
    try:
        results = await cognee.search(
            query, query_type=cognee.SearchType.CHUNKS, datasets=[dataset], node_name=[dataset]
        )
    except DatasetNotFoundError:
        # Nothing has been stored in this session yet — recall before the first add.
        return "No memories found."
    snippets = [text for text in (_chunk_text(r) for r in results or []) if text]
    if not snippets:
        return "No memories found."
    return "\n".join(snippets)


def get_sessionized_cognee_tools(session: str) -> tuple[Callable, Callable]:
    """Return ``(add, search)`` bound to ``session`` for isolated project memory.

    Mirrors the CrewAI integration's sessionized-tools factory: the returned
    callables take only the memory payload, so an Aider Python script can wire
    them in without threading the session id through every call.
    """
    return (
        functools.partial(add_project_memory, session),
        functools.partial(search_project_memory, session),
    )
