"""The transport seam between the Hermes provider and cognee.

The provider owns everything Hermes-shaped — scope routing, session-id
derivation, tool envelopes, the circuit breaker, the write gating. A backend owns
only *how the bytes reach cognee*. Keeping the split at exactly that line is what
lets the transport change without touching a single Hermes semantic.

The protocol is **synchronous** on purpose. cognee's SDK is async, so
:class:`SdkBackend` owns the event-loop thread; an HTTP backend needs no loop at
all. The provider therefore never learns that one of its transports happens to be
async, and the bridge disappears with the backend that needs it.

``SdkBackend`` is the in-process/served-SDK transport: it drives ``cognee.*``
directly, and in served mode routes through the SDK's own ``CloudClient``. Note
that this path silently drops ``session_ids`` on ``improve()`` (a CloudClient
limitation) — the reason a direct-HTTP backend exists.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import threading
from typing import Any, Optional

from .config import str_to_bool

logger = logging.getLogger(__name__)


def has_cognee() -> bool:
    """True when the cognee package is importable. Never imports it."""
    return importlib.util.find_spec("cognee") is not None


class MemoryBackend:
    """How the provider reaches cognee.

    Operations are required. The configuration hooks default to no-ops because
    they only mean something to an in-process SDK: an HTTP transport has no local
    model config, no local data roots, and no local user table.
    """

    # -- connection / lifecycle -------------------------------------------

    def configure_models(self, *, llm_api_key: str, llm_model: str) -> None:
        """Point the transport at an LLM (in-process transports only)."""

    def configure_local_roots(self, *, data_root: str, system_root: str) -> None:
        """Point the transport at local storage (in-process transports only)."""

    def connect(self, *, url: str, api_key: str, timeout: float) -> None:
        """Attach to a cognee instance at *url*."""
        raise NotImplementedError

    def resolve_identity(self, *, email: str, password: str, timeout: float) -> Any:
        """Resolve the principal this transport writes as, if it owns one."""
        return None

    def close(self, *, timeout: float = 5.0) -> None:
        """Release connections and background resources."""

    def connection_info(self) -> Optional[dict[str, Any]]:
        """How to reach the server this transport is attached to, if that server
        outlives this process (``url``, ``api_key``, ``agent_session_name``).

        None for in-process transports — there is nothing to close out after the
        process dies. The provider uses this to arm the crash-safe exit watcher.
        """
        return None

    # -- operations --------------------------------------------------------

    def recall(
        self,
        *,
        query: str,
        session_id: Optional[str],
        datasets: Optional[list[str]],
        top_k: int,
        auto_route: bool,
        query_type: Optional[str],
        timeout: float,
    ) -> list[Any]:
        raise NotImplementedError

    def remember_session(self, *, text: str, session_id: str, dataset: str, timeout: float) -> Any:
        """Store a turn in the session cache (cheap, no graph extraction)."""
        raise NotImplementedError

    def remember_permanent(
        self, *, text: str, dataset: str, session_ids: list[str], timeout: float
    ) -> Any:
        """Store content in the permanent graph."""
        raise NotImplementedError

    def forget(
        self,
        *,
        dataset: Optional[str],
        everything: bool,
        memory_only: bool,
        timeout: float,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def improve(
        self, *, dataset: str, session_ids: list[str], background: bool, timeout: float
    ) -> Any:
        """Bridge session-cache content into the permanent graph."""
        raise NotImplementedError

    # -- diagnostics -------------------------------------------------------

    def empty_recall_hint(self, *, timeout: float = 5.0) -> Optional[str]:
        """A reason recall *structurally* could not match, or None.

        Called only when a recall comes back empty, to distinguish a genuine miss
        from a misconfiguration. Best-effort: never raises.
        """
        return None


class _AsyncBridge:
    """Run cognee's async SDK from the provider's synchronous interface."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _ensure_loop(self) -> None:
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever,
                daemon=True,
                name="cognee-hermes-event-loop",
            )
            self._thread.start()

    def run(self, coro, timeout: float):
        self._ensure_loop()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def shutdown(self) -> None:
        with self._lock:
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=5.0)
            self._loop = None
            self._thread = None


class SdkBackend(MemoryBackend):
    """Drives the cognee Python SDK, either in-process or via ``cognee.serve()``.

    ``served`` records whether :meth:`connect` ran. It gates two things that only
    make sense in-process: the local user identity (a served instance owns
    identity via the api-key principal) and the embedding-dimension probe (a
    served instance owns the vector store).
    """

    def __init__(self) -> None:
        self._bridge = _AsyncBridge()
        self._user: Any = None
        self.served = False

    @property
    def user(self) -> Any:
        return self._user

    # -- connection / lifecycle -------------------------------------------

    def configure_models(self, *, llm_api_key: str, llm_model: str) -> None:
        try:
            import cognee

            if llm_api_key:
                cognee.config.set_llm_api_key(str(llm_api_key))
            if llm_model:
                cognee.config.set_llm_model(str(llm_model))
        except Exception as exc:
            logger.debug("Cognee model configuration failed: %s", exc)

    def configure_local_roots(self, *, data_root: str, system_root: str) -> None:
        # Embedded mode runs cognee's session manager in *this* process, and it is
        # gated on CACHING: without it a session write is silently dropped while
        # still reporting success, so turns never reach the graph. The spawned
        # server gets the same flags in server_bootstrap._spawn. setdefault so an
        # explicit user value wins.
        for key, value in (("CACHING", "true"), ("AUTO_FEEDBACK", "true")):
            os.environ.setdefault(key, value)
        try:
            import cognee

            cognee.config.data_root_directory(str(data_root))
            cognee.config.system_root_directory(str(system_root))
        except Exception as exc:
            logger.debug("Cognee root configuration failed: %s", exc)

    def connect(self, *, url: str, api_key: str, timeout: float) -> None:
        self._bridge.run(self._do_serve(url, api_key), timeout=timeout)
        self.served = True

    def resolve_identity(self, *, email: str, password: str, timeout: float) -> Any:
        self._user = self._bridge.run(self._ensure_identity(email, password), timeout=timeout)
        return self._user

    def close(self, *, timeout: float = 5.0) -> None:
        if self.served:
            try:
                self._bridge.run(self._do_disconnect(), timeout=timeout)
            except Exception:
                pass
        self._bridge.shutdown()

    # -- operations --------------------------------------------------------

    def recall(
        self,
        *,
        query,
        session_id,
        datasets,
        top_k,
        auto_route,
        query_type,
        timeout,
    ) -> list[Any]:
        return self._bridge.run(
            self._do_recall(query, session_id, datasets, top_k, auto_route, query_type),
            timeout=timeout,
        )

    def remember_session(self, *, text, session_id, dataset, timeout) -> Any:
        return self._bridge.run(
            self._do_remember_session(text, session_id, dataset), timeout=timeout
        )

    def remember_permanent(self, *, text, dataset, session_ids, timeout) -> Any:
        return self._bridge.run(
            self._do_remember_permanent(text, dataset, session_ids), timeout=timeout
        )

    def forget(self, *, dataset, everything, memory_only, timeout) -> dict[str, Any]:
        return self._bridge.run(
            self._do_forget(dataset, everything=everything, memory_only=memory_only),
            timeout=timeout,
        )

    def improve(self, *, dataset, session_ids, background, timeout) -> Any:
        return self._bridge.run(
            self._do_improve(dataset, session_ids, run_in_background=background),
            timeout=timeout,
        )

    # -- diagnostics -------------------------------------------------------

    def empty_recall_hint(self, *, timeout: float = 5.0) -> Optional[str]:
        """Confirmed embedding-dimension mismatch, or None. In-process only.

        A served instance owns the vector store, so introspecting the local
        in-process engine would be meaningless — skip.
        """
        if self.served:
            return None
        try:
            return self._bridge.run(dimension_mismatch_hint(), timeout=timeout)
        except Exception:
            return None

    # -- the async SDK surface ---------------------------------------------

    def _add_user_kwarg(self, kwargs: dict[str, Any]) -> None:
        """Inject the local user only when running in-process.

        In served mode the instance owns identity (api-key principal) and
        ``self._user`` is None. Passing ``user=None`` is not the same as omitting
        the key — the SDK may treat an explicit None differently (overriding a
        default, affecting tenant scoping) — so we omit it entirely instead.
        """
        if not self.served and self._user is not None:
            kwargs["user"] = self._user

    async def _ensure_identity(self, email: str, password: str):
        try:
            from cognee.modules.users.methods import (
                create_user,
                get_default_user,
                get_user_by_email,
            )
        except Exception:
            return None

        user = await get_user_by_email(email)
        if user:
            return user
        try:
            return await create_user(
                email=email,
                password=password,
                is_verified=True,
                is_active=True,
            )
        except Exception:
            user = await get_user_by_email(email)
            if user:
                return user
            return await get_default_user()

    async def _do_serve(self, url: str, api_key: str):
        import cognee

        kwargs = {"url": url}
        if api_key:
            kwargs["api_key"] = api_key
        return await cognee.serve(**kwargs)

    async def _do_disconnect(self):
        import cognee

        return await cognee.disconnect()

    async def _do_recall(
        self,
        query: str,
        session_id: Optional[str],
        datasets: Optional[list[str]],
        top_k: int,
        auto_route: bool,
        query_type: Optional[str],
    ) -> list[Any]:
        import cognee

        kwargs: dict[str, Any] = {
            "top_k": top_k,
            "auto_route": auto_route,
        }
        self._add_user_kwarg(kwargs)
        if session_id is not None:
            kwargs["session_id"] = session_id
        if datasets is not None:
            kwargs["datasets"] = datasets
        if query_type:
            kwargs["query_type"] = resolve_search_type(query_type)

        return await cognee.recall(query_text=query, **kwargs)

    async def _do_remember_session(self, content: str, session_id: str, dataset: str):
        import cognee

        kwargs: dict[str, Any] = {
            "data": content,
            "dataset_name": dataset,
            "session_id": session_id,
            "self_improvement": False,
        }
        self._add_user_kwarg(kwargs)
        return await cognee.remember(**kwargs)

    async def _do_remember_permanent(self, content: str, dataset: str, session_ids: list[str]):
        import cognee

        kwargs: dict[str, Any] = {
            "data": content,
            "dataset_name": dataset,
            "self_improvement": True,
            "session_ids": session_ids,
        }
        self._add_user_kwarg(kwargs)
        return await cognee.remember(**kwargs)

    async def _do_forget(
        self,
        dataset: Optional[str],
        *,
        everything: bool = False,
        memory_only: bool = False,
    ) -> dict[str, Any]:
        import cognee

        kwargs: dict[str, Any] = {
            "everything": everything,
            "memory_only": memory_only,
        }
        if dataset and not everything:
            kwargs["dataset"] = dataset
        self._add_user_kwarg(kwargs)
        return await cognee.forget(**kwargs)

    async def _do_improve(
        self, dataset: str, session_ids: list[str], run_in_background: bool = False
    ):
        import cognee

        kwargs: dict[str, Any] = {
            "dataset": dataset,
            "session_ids": session_ids,
            "run_in_background": run_in_background,
        }
        self._add_user_kwarg(kwargs)
        return await cognee.improve(**kwargs)


# --- Embedding-dimension mismatch detection ---------------------------------
# When the embedding model changes between writing and reading, stored vectors
# and query vectors differ in size, so recall silently matches nothing. These
# helpers turn that silent miss into a one-line actionable error naming both
# dimensions and the active embedder. Best-effort and fail-safe (any uncertainty
# returns None). Only valid in-process, where this process owns the vector store;
# a served instance owns it and the in-process engine here would not reflect it.


async def sample_stored_vector_dim(engine) -> Optional[int]:
    """Sample the dimension of a stored vector from any populated collection, or None.

    Enumerates the store's actual collections via the vector interface's
    ``get_connection().table_names()`` (the same path cognee's own ``has_collection``
    uses) rather than assuming fixed collection names, so it also covers custom
    pipelines. Never raises: each collection is probed independently and any
    unreadable one is skipped. Covers cognee's default local backend (LanceDB); other
    backends whose connection can't enumerate return None and fall back to the normal
    empty-recall path.
    """
    try:
        connection = await engine.get_connection()
        names = await connection.table_names()
    except Exception:
        return None
    for name in names:
        try:
            collection = await engine.get_collection(name)
            rows = await collection.query().limit(1).to_list()
            if rows:
                vector = rows[0].get("vector")
                if vector is not None:
                    return len(vector)
        except Exception:
            continue
    return None


async def dimension_mismatch_hint(engine=None) -> Optional[str]:
    """One-line diagnostic when the stored vectors differ in size from the active
    embedder's query vectors (so recall can never match), else None.

    ``engine`` is injectable for testing. Best-effort and fail-safe: any error, or
    an indeterminate/matching dimension, returns None so the caller keeps the
    normal empty-recall behavior.
    """
    try:
        if engine is None:
            from cognee.infrastructure.databases.vector import get_vector_engine

            engine = get_vector_engine()
        embed = getattr(engine, "embedding_engine", None)
        if embed is None:
            return None
        query_dim = int(embed.get_vector_size())
        stored_dim = await sample_stored_vector_dim(engine)
        if not stored_dim or not query_dim or stored_dim == query_dim:
            return None
        model = getattr(embed, "model", None) or "unknown-model"
        provider = getattr(embed, "provider", None) or "unknown-provider"
        return (
            "Cognee recall found nothing because the embedder changed: stored vectors are "
            f"{stored_dim}-d but the active embedder '{model}' (provider '{provider}') produces "
            f"{query_dim}-d queries. Re-index this data with the current embedder, or set "
            f"EMBEDDING_MODEL/EMBEDDING_DIMENSIONS back to the {stored_dim}-d model that wrote it."
        )
    except Exception:
        return None


def resolve_search_type(search_type: str):
    """Map a search-type name onto cognee's ``SearchType``, defaulting sanely.

    An unrecognized name falls back to ``GRAPH_COMPLETION`` rather than failing the
    recall. Kept in the backend so the provider needs no cognee import — and so a
    future cognee search type keeps working without a provider change.
    """
    from cognee.modules.search.types import SearchType

    key = str(search_type).upper().strip()
    return getattr(SearchType, key, SearchType.GRAPH_COMPLETION)


def build_backend(config: Optional[dict[str, Any]] = None):
    """Pick a transport from config.

    Direct HTTP is the default: it is what the other cognee plugins use, and the
    only transport that sends ``session_ids`` on ``improve()`` — the SDK's
    ``CloudClient`` drops them, so the session-to-graph bridge silently becomes a
    dataset-wide improve.

    The SDK transport is selected by ``COGNEE_EMBEDDED=true``, which it is the only
    one able to serve (it runs cognee in this process, with no server involved), or
    explicitly by ``COGNEE_TRANSPORT=sdk`` for a like-for-like comparison.
    """
    config = config or {}
    transport = str(config.get("transport") or "").strip().lower()
    if transport in {"sdk", "cognee"}:
        return SdkBackend()
    # Embedded means "no server" — only the in-process SDK can do that, so it wins
    # over the default regardless of transport.
    if str_to_bool(config.get("embedded"), False):
        return SdkBackend()

    from .http_backend import HttpBackend

    return HttpBackend()


def default_backend() -> MemoryBackend:
    """The transport used before config is loaded, or when none is injected.

    Deliberately the SDK: it needs no URL, no key and no reachable server, so
    constructing a provider is side-effect free. ``initialize()`` replaces it with
    the configured transport once it knows what to build.
    """
    return SdkBackend()
