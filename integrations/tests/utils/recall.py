"""Driver for ``session-context-lookup.py``'s ``_run`` — the prompt hot path.

``_run`` is where a prompt turns into memory: it fans out over four recall scopes,
folds each call's outcome back into the shared connection state, feeds the circuit
breaker, and emits the ``context_lookup_*`` event the status line reads. Testing
it needs every one of those seams captured at once, which is why this lives here
rather than being re-stubbed per file.

Everything is driven in **cloud (HTTP) mode**, because that is the only mode where
the health accounting runs — local-SDK mode has no request to learn from.

The stubs are installed with ``monkeypatch.setattr`` at its default
``raising=True``: if a seam is renamed in one integration and not the other, the
driver fails loudly instead of quietly testing nothing.
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from typing import Any, Callable

#: The scopes ``_run`` fans out over, in canonical (reporting) order. The
#: scopes are dispatched concurrently, so this is the order of ``per_scope`` and
#: of the injected sections, not an order of calls. The optional ``code`` lane
#: is added only on prompts that arm it, so it is not part of the always-present
#: set.
SCOPES = ("session", "trace", "session_context", "graph")

#: Base URL every driven run resolves to. Health state is keyed by service URL
#: (SDK-356), so assertions need the exact value the hook was handed.
URL = "https://cloud.example"

#: A session key in the shape the hooks accept, shared by the header tests.
SESSION_KEY = "fde122ae-07db-431d-b5af-acba353e4e3e"

#: One session hit and nothing else — the smallest recall that counts as a hit.
HIT = {
    "session": [{"question": "q1", "answer": "a1"}],
    "trace": [],
    "graph": [],
    "session_context": [],
}


def load_lookup(
    suite,
    hook_module,
    monkeypatch,
    *,
    session_key: str = SESSION_KEY,
    status_line: str = "cognee: ds · local",
):
    """``session-context-lookup.py`` with the session key pinned.

    Hosts that prefix the memory header with their plain status line (the
    Codex-derived cores) get that line held inert, so header assertions see a
    fixed prefix instead of whatever the renderer reads from the temp HOME.
    """
    module = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(module, "get_session_key", lambda: session_key)
    if hasattr(module, "render_status_for_host"):
        monkeypatch.setattr(module, "render_status_for_host", lambda key: status_line)
    return module


#: A connection-state marker standing for "this server has answered before",
#: which is what separates a real outage from an ordinary cold start.
READY_PRIOR = {"state": "ready", "base_url": URL, "checked_at": 1.0}


@dataclass
class RecallRun:
    """Everything one ``_run`` invocation did, captured for assertion."""

    #: ``(event, detail)`` for every ``hook_log`` call, in order.
    events: list[tuple[str, dict]] = field(default_factory=list)
    #: ``(state, url, detail)`` per connection-state write; ``mark_server_ready``
    #: is recorded as ``("ready", url, "")`` so both land in one ordered list.
    writes: list[tuple[str, str, str]] = field(default_factory=list)
    #: ``("success", url)`` / ``("failure", url, reason)`` breaker accounting.
    breaker: list[tuple] = field(default_factory=list)
    #: Scope names actually dispatched. The scopes run concurrently (each call
    #: lands here from its own worker thread), so the ORDER of this list is not
    #: meaningful — assert on membership and length.
    calls: list[str] = field(default_factory=list)
    #: ``{scope: timeout}`` as handed to ``recall_via_http``, for budget clamping.
    timeouts: dict[str, float] = field(default_factory=dict)
    #: ``{scope: kwargs}`` as handed to ``recall_via_http`` — the code lane's
    #: dataset/code_query override is only visible here.
    kwargs: dict[str, dict] = field(default_factory=dict)
    #: Whatever ``_run`` returned (the injected context, or None).
    output: Any = None

    def detail(self, event: str) -> dict | None:
        """The detail dict of the first ``event``, or None if it never fired."""
        for name, detail in self.events:
            if name == event:
                return detail
        return None

    def fired(self, event: str) -> bool:
        return any(name == event for name, _ in self.events)


def drive_recall(
    module: types.ModuleType,
    monkeypatch,
    *,
    recall: Callable[..., list] | dict[str, list] | None = None,
    prompt: str = "please recall something relevant",
    breaker_open: tuple[bool, int] = (False, 0),
    prior_state: dict | None = None,
    ready_hint: bool = False,
    slow_streak: int = 1,
    slow_threshold: int = 3,
    cwd: str = "",
    mode: str = "http",
    sdk_recall: Callable[..., Any] | dict[str, list] | None = None,
    saves_last_turn: dict | None = None,
) -> RecallRun:
    """Run ``module._run(prompt)`` with every seam captured.

    ``recall`` is either a callable used as ``recall_via_http``, or a
    ``{scope: results}`` map for the common case of fixed per-scope results.
    ``prior_state``/``ready_hint`` set what the hook believes about the server
    before the attempt; ``slow_streak``/``slow_threshold`` drive timeout
    escalation without touching real streak files.

    ``saves_last_turn`` is what the hook reads back from the save counter (all
    kinds zero by default), so a header test can hand it buffered writes.

    ``mode="http"`` (the default) drives cloud/HTTP mode, the only mode where
    the health accounting runs. ``mode="local_sdk"`` drives the in-process SDK
    branch of suites that still carry one (``Suite.has_local_sdk_recall``): a
    fake ``cognee`` package is installed whose ``recall`` coroutine is
    ``sdk_recall`` — an ``async`` callable ``(prompt, **kwargs)`` or a
    ``{scope: results}`` map — and the local-only seams (readiness, user
    resolution, the trace fallback and the dim-mismatch probe) are stubbed to
    no-ops so the test sees the fan-out alone. Calls land in ``run.calls`` /
    ``run.kwargs`` either way; ``run.timeouts`` is HTTP-only, because the SDK
    branch bounds each call with ``asyncio.wait_for`` rather than a kwarg.
    """
    run = RecallRun()
    local_sdk = mode == "local_sdk"
    if mode not in ("http", "local_sdk"):
        raise ValueError(f"unknown drive_recall mode: {mode!r}")

    if recall is None:
        recall = {}
    if isinstance(recall, dict):
        results = recall

        def _recall_fn(_prompt, **kw):
            return list(results.get(kw["scope"][0], []))
    else:
        _recall_fn = recall

    def _recall(prompt_arg, **kw):
        # Called from the hook's worker threads, one per scope. list.append and
        # dict item assignment are atomic under the GIL, so no lock is needed.
        scope = kw["scope"][0]
        run.calls.append(scope)
        if "timeout" in kw:
            run.timeouts[scope] = kw["timeout"]
        run.kwargs[scope] = dict(kw)
        return _recall_fn(prompt_arg, **kw)

    seams = {
        "hook_log": lambda event, detail=None: run.events.append((event, detail or {})),
        "notify": lambda *a, **k: None,
        "load_config": lambda: {},
        "resolve_runtime_mode": (
            (lambda: {"mode": "local_sdk", "base_url": ""})
            if local_sdk
            else (lambda: {"mode": "http", "base_url": URL})
        ),
        "read_connection_state": lambda: dict(prior_state or {}),
        "server_ready_hint": lambda url: ready_hint,
        "mark_server_ready": lambda url: run.writes.append(("ready", url, "")),
        "write_connection_state": lambda state, url, detail="": run.writes.append(
            (state, url, detail)
        ),
        "clear_slow_streak": lambda url: None,
        "record_slow_probe": lambda url: slow_streak,
        "slow_streak_threshold": lambda: slow_threshold,
        "_load_session_id": lambda: "sid",
        "read_and_reset_save_counter": lambda sid: dict(
            saves_last_turn or {"prompt": 0, "trace": 0, "answer": 0}
        ),
        "recall_via_http": _recall,
    }
    for name, impl in seams.items():
        monkeypatch.setattr(module, name, impl)

    if local_sdk:
        _install_local_sdk(module, monkeypatch, run, sdk_recall)

    # ``_run`` imports the breaker lazily in cloud mode, so a fake in sys.modules
    # shadows the real one and keeps on-disk breaker state out of the test.
    fake_client = types.ModuleType("_cognee_client")
    fake_client.breaker_open = lambda service_url="": breaker_open
    fake_client.record_success = lambda service_url="": run.breaker.append(("success", service_url))
    fake_client.record_failure = lambda error="", now=None, service_url="", reason="": (
        run.breaker.append(("failure", service_url, reason))
    )
    monkeypatch.setitem(sys.modules, "_cognee_client", fake_client)

    run.output = asyncio.run(module._run(prompt, cwd))
    return run


async def _noop_async(*_args, **_kwargs):
    return None


async def _empty_async(*_args, **_kwargs):
    return []


def _install_local_sdk(module, monkeypatch, run: RecallRun, sdk_recall) -> None:
    """Stub the local-SDK seams and install a fake ``cognee`` for ``_run``.

    The hook imports ``cognee`` and ``cognee.modules.search.types.SearchType``
    lazily inside ``_run``, so entries in ``sys.modules`` are what it sees. The
    recorded ``scope`` is the first element of the ``scope`` kwarg, matching the
    HTTP driver, so ``run.calls`` reads the same in both modes.
    """
    if sdk_recall is None:
        sdk_recall = {}
    if isinstance(sdk_recall, dict):
        fixed = sdk_recall

        async def _sdk_fn(_prompt, **kw):
            return list(fixed.get(kw["scope"][0], []))
    else:
        _sdk_fn = sdk_recall

    async def _cognee_recall(prompt_arg, **kw):
        scope = kw["scope"][0]
        run.calls.append(scope)
        run.kwargs[scope] = dict(kw)
        return await _sdk_fn(prompt_arg, **kw)

    # Local-only seams. ``raising=True`` on purpose: a suite that claims the
    # local-SDK capability must carry every one of these.
    for name, impl in {
        "ensure_cognee_ready": _noop_async,
        "resolve_user": _noop_async,
        "_load_user_id": lambda: "user-1",
        "_recent_trace_fallback": _empty_async,
        "service_url_is_local": lambda url="": False,
        "bounded_dim_mismatch_hint": _noop_async,
    }.items():
        monkeypatch.setattr(module, name, impl)

    search_types = types.SimpleNamespace(
        HYBRID_COMPLETION="HYBRID_COMPLETION", GRAPH_COMPLETION="GRAPH_COMPLETION"
    )
    cognee = types.ModuleType("cognee")
    cognee.recall = _cognee_recall
    cognee_modules = types.ModuleType("cognee.modules")
    cognee_search = types.ModuleType("cognee.modules.search")
    cognee_types = types.ModuleType("cognee.modules.search.types")
    cognee_types.SearchType = search_types
    cognee.modules = cognee_modules
    cognee_modules.search = cognee_search
    cognee_search.types = cognee_types
    for name, mod in {
        "cognee": cognee,
        "cognee.modules": cognee_modules,
        "cognee.modules.search": cognee_search,
        "cognee.modules.search.types": cognee_types,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


def assert_valid_per_scope(per_scope: dict) -> None:
    """Every scope reports, in canonical order, with a numeric non-negative time.

    A scope missing from the breakdown is the failure this guards: the point of
    per-scope instrumentation is that a scope which returned nothing or never ran
    is still visible, rather than vanishing from the record.
    """
    assert list(per_scope.keys()) == list(SCOPES), f"expected all four scopes in order: {per_scope}"
    for label, record in per_scope.items():
        assert isinstance(record["hits"], int), f"{label} hits not an int: {record}"
        assert isinstance(record["elapsed_ms"], (int, float)), f"{label} elapsed: {record}"
        assert record["elapsed_ms"] >= 0, f"{label} negative elapsed: {record}"
