"""Shared stubbing for the characterization suite.

The characterization tests pin the behaviour the HTTP refactor must preserve:
the JSON envelopes Hermes hands to the model, the request payloads the provider
builds, the session-id derivation, and the lifecycle-hook semantics that exist
only in Hermes (``agent_context`` write gating, the prefetch two-phase protocol,
``on_memory_write``, ``on_delegation``).

**One seam.** Every cognee-touching call the provider makes goes through a
call-time ``import cognee``, so this module fakes the ``cognee`` module itself
rather than patching provider internals. That has two useful properties:

1. The recorded kwargs are exactly what the provider asks cognee to do — which
   is the contract the HTTP payloads must reproduce field for field.
2. When the backend protocol lands, **this file is the only one that should need
   to change.** The assertions live in the test modules and are written against
   provider inputs and outputs, never against cognee.

None of these tests need the real cognee installed, and none of them touch the
network or the filesystem outside ``tmp_path``.
"""

import asyncio
import contextlib
import sys
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cognee_integration_hermes import provider as provider_mod  # noqa: E402

# Module paths the fake has to occupy for ``from cognee.x.y import z`` to
# resolve. Keep in sync with the call-time imports in provider.py.
_FAKE_MODULE_PATHS = (
    "cognee",
    "cognee.modules",
    "cognee.modules.search",
    "cognee.modules.search.types",
)


class RememberResultStub:
    """Stands in for cognee's ``RememberResult`` (an object with ``.status``)."""

    def __init__(self, status="completed"):
        self.status = status


class _SearchType:
    """Enough of cognee's ``SearchType`` for ``_resolve_search_type`` to work.

    Attribute lookup returns the name as a plain string so recorded
    ``query_type`` values stay readable in assertions.
    """

    GRAPH_COMPLETION = "GRAPH_COMPLETION"
    RAG_COMPLETION = "RAG_COMPLETION"
    CHUNKS = "CHUNKS"
    CHUNKS_LEXICAL = "CHUNKS_LEXICAL"
    TEMPORAL = "TEMPORAL"
    FEELING_LUCKY = "FEELING_LUCKY"


class _FakeCogneeConfig:
    def __init__(self, recorder):
        self._recorder = recorder

    def set_llm_api_key(self, value):
        self._recorder.record("config.set_llm_api_key", {"value": value})

    def set_llm_model(self, value):
        self._recorder.record("config.set_llm_model", {"value": value})

    def data_root_directory(self, value):
        self._recorder.record("config.data_root_directory", {"value": value})

    def system_root_directory(self, value):
        self._recorder.record("config.system_root_directory", {"value": value})


class FakeCognee:
    """Records what the provider asks cognee to do, and what it hands back.

    ``results[name]`` sets the return value for an operation; ``errors[name]``
    makes it raise. Threaded provider paths (``sync_turn``, ``on_memory_write``,
    ``queue_prefetch``) are awaited with :meth:`wait`.
    """

    def __init__(self):
        self.calls = []
        self.results = {
            "recall": [],
            "remember": RememberResultStub(),
            "improve": {},
            "forget": {"deleted": True},
            "serve": None,
            "disconnect": None,
        }
        self.errors = {}
        self.config = _FakeCogneeConfig(self)
        self._lock = threading.Lock()
        self._events = {}

    # -- recording ---------------------------------------------------------

    def record(self, name, kwargs):
        with self._lock:
            self.calls.append((name, kwargs))
            event = self._events.setdefault(name, threading.Event())
        event.set()
        error = self.errors.get(name)
        if error is not None:
            raise error
        return self.results.get(name)

    def _event(self, name):
        with self._lock:
            return self._events.setdefault(name, threading.Event())

    def wait(self, name, timeout=2.0):
        """Block until *name* has been called at least once. Returns True on success."""
        return self._event(name).wait(timeout)

    # -- inspection --------------------------------------------------------

    def names(self):
        return [name for name, _ in self.calls]

    def kwargs_for(self, name):
        return [kwargs for called, kwargs in self.calls if called == name]

    def only_call(self, name):
        """The kwargs of the single call to *name*; fails loudly if not exactly one."""
        matches = self.kwargs_for(name)
        if len(matches) != 1:
            raise AssertionError(f"expected exactly 1 {name!r} call, got {len(matches)}")
        return matches[0]

    # -- the async surface provider.py awaits ------------------------------

    async def recall(self, **kwargs):
        return self.record("recall", kwargs)

    async def remember(self, **kwargs):
        return self.record("remember", kwargs)

    async def improve(self, **kwargs):
        return self.record("improve", kwargs)

    async def forget(self, **kwargs):
        return self.record("forget", kwargs)

    async def serve(self, **kwargs):
        return self.record("serve", kwargs)

    async def disconnect(self, **kwargs):
        return self.record("disconnect", kwargs)


class _SyncBridge:
    """Runs the provider's coroutines inline — no event-loop thread in tests."""

    def run(self, coro, timeout=None):
        return asyncio.run(coro)

    def shutdown(self):
        pass


@contextlib.contextmanager
def fake_cognee():
    """Install a :class:`FakeCognee` as the ``cognee`` package for the duration."""
    recorder = FakeCognee()

    modules = {}
    root = types.ModuleType("cognee")
    root.recall = recorder.recall
    root.remember = recorder.remember
    root.improve = recorder.improve
    root.forget = recorder.forget
    root.serve = recorder.serve
    root.disconnect = recorder.disconnect
    root.config = recorder.config
    modules["cognee"] = root

    for path in _FAKE_MODULE_PATHS[1:]:
        modules[path] = types.ModuleType(path)
    modules["cognee.modules.search.types"].SearchType = _SearchType
    # Parent-attribute wiring, so both ``import a.b`` and ``from a.b import c`` work.
    modules["cognee"].modules = modules["cognee.modules"]
    modules["cognee.modules"].search = modules["cognee.modules.search"]
    modules["cognee.modules.search"].types = modules["cognee.modules.search.types"]

    saved = {path: sys.modules.get(path) for path in _FAKE_MODULE_PATHS}
    sys.modules.update(modules)
    try:
        yield recorder
    finally:
        for path, original in saved.items():
            if original is None:
                sys.modules.pop(path, None)
            else:
                sys.modules[path] = original


def make_provider(
    *,
    dataset="hermes",
    top_k=5,
    session_id="s-1",
    session_cognee_id=None,
    remote_mode=True,
    writes_enabled=True,
    auto_route=True,
    improve_on_end=True,
    config=None,
):
    """A provider wired for inline execution, with post-``initialize()`` state set.

    ``remote_mode`` defaults to True so the embedded-only dimension-mismatch
    probe short-circuits — ``test_dim_mismatch.py`` already covers that gate, and
    it disappears with the SDK.
    """
    provider = provider_mod.CogneeMemoryProvider()
    provider._bridge = _SyncBridge()
    provider._initialized = True
    provider._remote_mode = remote_mode
    provider._writes_enabled = writes_enabled
    provider._dataset = dataset
    provider._top_k = top_k
    provider._auto_route = auto_route
    provider._improve_on_end = improve_on_end
    provider._session_id = session_id
    provider._session_cognee_id = session_cognee_id or f"hermes_{session_id}"
    provider._config = {
        "recall_timeout": 5,
        "write_timeout": 5,
        "improve_timeout": 5,
        "improve_background": "",
        **(config or {}),
    }
    return provider
