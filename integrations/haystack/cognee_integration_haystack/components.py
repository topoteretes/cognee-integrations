import os
from typing import Any, Dict, List, Optional

from haystack import Document, component, default_from_dict, default_to_dict

from .runtime import recall, remember, render_results


def _default_dataset() -> Optional[str]:
    """``COGNEE_PROJECT_NODE_SET`` is the project-scoping env var the other
    cognee integrations use. Those sit on the older ``add``/``cognify``/
    ``search`` API, whose scoping kwarg is ``node_set``; ``remember``/``recall``
    (what this integration and the strands one call) don't accept ``node_set``
    at all; their equivalent is a dataset name (``dataset_name`` to write,
    ``datasets`` to read). Same env var, mapped onto the parameter this API
    generation actually has.
    """
    tag = os.environ.get("COGNEE_PROJECT_NODE_SET", "").strip()
    return tag or None


@component
class CogneeRetriever:
    """Retrieves ``Document``s from cognee's knowledge graph for a Haystack ``Pipeline``.

    Implements Haystack's retriever protocol (a ``run(query, top_k=None)`` that
    returns ``{"documents": [...]}``), so it slots into a ``Pipeline`` next to
    any other retriever instead of being a standalone function call.
    """

    def __init__(
        self,
        datasets: Optional[List[str]] = None,
        top_k: int = 15,
        recall_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        :param datasets: Cognee dataset names to search. Defaults to
            ``[COGNEE_PROJECT_NODE_SET]`` when that env var is set, else
            cognee's own default (whatever datasets the account has).
        :param top_k: Default number of results; overridable per call. Forwarded
            straight to ``cognee.recall``'s own ``top_k``, so cognee does the
            limiting, not this component.
        :param recall_kwargs: Extra kwargs forwarded to every ``cognee.recall``
            call (e.g. ``session_id``, ``query_type``). No defaults are imposed
            beyond dataset scoping.
        """
        default_dataset = _default_dataset()
        if datasets is not None:
            self._datasets = datasets
        else:
            self._datasets = [default_dataset] if default_dataset else None
        self._top_k = top_k
        self._recall_kwargs = dict(recall_kwargs or {})

    @component.output_types(documents=List[Document])
    def run(self, query: str, top_k: Optional[int] = None) -> Dict[str, List[Document]]:
        """
        :param query: A natural-language search query.
        :param top_k: Overrides the retriever's default result count for this call.
        """
        kwargs = dict(self._recall_kwargs)
        if self._datasets:
            kwargs.setdefault("datasets", self._datasets)
        kwargs.setdefault("top_k", top_k if top_k is not None else self._top_k)
        results = recall(query, **kwargs)
        documents = [Document(content=text) for text in render_results(results)]
        return {"documents": documents}

    def to_dict(self) -> Dict[str, Any]:
        return default_to_dict(
            self,
            datasets=self._datasets,
            top_k=self._top_k,
            recall_kwargs=self._recall_kwargs,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CogneeRetriever":
        return default_from_dict(cls, data)


@component
class CogneeWriter:
    """Pushes ``Document``s a Haystack ``Pipeline`` produces into cognee's knowledge graph.

    Implements Haystack's writer protocol (a ``run(documents)`` that returns
    ``{"documents_written": n}``, matching ``DocumentWriter``'s output shape),
    so write steps already in a pipeline (converters, splitters, embedders) can
    feed cognee the same way they'd feed a document store.
    """

    def __init__(
        self,
        dataset_name: Optional[str] = None,
        remember_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        :param dataset_name: Cognee dataset to write into. Defaults to
            ``COGNEE_PROJECT_NODE_SET`` when set, else cognee's own
            ``"main_dataset"`` default.
        :param remember_kwargs: Extra kwargs forwarded to every ``cognee.remember``
            call (e.g. ``session_id``).
        """
        self._dataset_name = dataset_name if dataset_name is not None else _default_dataset()
        self._remember_kwargs = dict(remember_kwargs or {})

    @component.output_types(documents_written=int)
    def run(self, documents: List[Document]) -> Dict[str, int]:
        """
        :param documents: Documents to store. ``Document.content`` is what
            cognee ingests; other fields (meta, embedding) are not forwarded,
            since cognee builds its own graph representation from the text.
        """
        kwargs = dict(self._remember_kwargs)
        if self._dataset_name:
            kwargs.setdefault("dataset_name", self._dataset_name)
        written = 0
        for doc in documents:
            if not doc.content:
                continue
            remember(doc.content, **kwargs)
            written += 1
        return {"documents_written": written}

    def to_dict(self) -> Dict[str, Any]:
        return default_to_dict(
            self,
            dataset_name=self._dataset_name,
            remember_kwargs=self._remember_kwargs,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CogneeWriter":
        return default_from_dict(cls, data)
