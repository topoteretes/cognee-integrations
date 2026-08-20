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

#: The scopes ``_run`` fans out over, in dispatch order.
SCOPES = ("session", "trace", "session_context", "graph")

#: Base URL every driven run resolves to. Health state is keyed by service URL
#: (SDK-356), so assertions need the exact value the hook was handed.
URL = "https://cloud.example"

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
    #: Scope names actually dispatched — the length is how much budget was spent.
    calls: list[str] = field(default_factory=list)
    #: ``{scope: timeout}`` as handed to ``recall_via_http``, for budget clamping.
    timeouts: dict[str, float] = field(default_factory=dict)
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
) -> RecallRun:
    """Run ``module._run(prompt)`` in cloud mode with every seam captured.

    ``recall`` is either a callable used as ``recall_via_http``, or a
    ``{scope: results}`` map for the common case of fixed per-scope results.
    ``prior_state``/``ready_hint`` set what the hook believes about the server
    before the attempt; ``slow_streak``/``slow_threshold`` drive timeout
    escalation without touching real streak files.
    """
    run = RecallRun()

    if recall is None:
        recall = {}
    if isinstance(recall, dict):
        results = recall

        def _recall_fn(_prompt, **kw):
            return list(results.get(kw["scope"][0], []))
    else:
        _recall_fn = recall

    def _recall(prompt_arg, **kw):
        scope = kw["scope"][0]
        run.calls.append(scope)
        if "timeout" in kw:
            run.timeouts[scope] = kw["timeout"]
        return _recall_fn(prompt_arg, **kw)

    seams = {
        "hook_log": lambda event, detail=None: run.events.append((event, detail or {})),
        "notify": lambda *a, **k: None,
        "load_config": lambda *a, **k: {},
        "resolve_runtime_mode": lambda: {"mode": "http", "base_url": URL},
        "read_connection_state": lambda: dict(prior_state or {}),
        "server_ready_hint": lambda url: ready_hint,
        "mark_server_ready": lambda url: run.writes.append(("ready", url, "")),
        "write_connection_state": lambda state, url, detail="": run.writes.append(
            (state, url, detail)
        ),
        "clear_slow_streak": lambda url: None,
        "record_slow_probe": lambda url: slow_streak,
        "slow_streak_threshold": lambda: slow_threshold,
        "_load_session": lambda workspace="": ("sid", "agent_sessions"),
        "read_and_reset_save_counter": lambda sid: {"prompt": 0, "trace": 0, "answer": 0},
        "recall_via_http": _recall,
    }
    for name, impl in seams.items():
        monkeypatch.setattr(module, name, impl)

    # ``_run`` imports the breaker lazily in cloud mode, so a fake in sys.modules
    # shadows the real one and keeps on-disk breaker state out of the test.
    fake_client = types.ModuleType("_cognee_client")
    fake_client.breaker_open = lambda service_url="": breaker_open
    fake_client.record_success = lambda service_url="": run.breaker.append(("success", service_url))
    fake_client.record_failure = lambda error="", now=None, service_url="", reason="": (
        run.breaker.append(("failure", service_url, reason))
    )
    monkeypatch.setitem(sys.modules, "_cognee_client", fake_client)

    run.output = asyncio.run(module._run(prompt))
    return run


def assert_valid_per_scope(per_scope: dict) -> None:
    """Every scope reports, in dispatch order, with a numeric non-negative time.

    A scope missing from the breakdown is the failure this guards: the point of
    per-scope instrumentation is that a scope which returned nothing or never ran
    is still visible, rather than vanishing from the record.
    """
    assert list(per_scope.keys()) == list(SCOPES), f"expected all four scopes in order: {per_scope}"
    for label, record in per_scope.items():
        assert isinstance(record["hits"], int), f"{label} hits not an int: {record}"
        assert isinstance(record["elapsed_ms"], (int, float)), f"{label} elapsed: {record}"
        assert record["elapsed_ms"] >= 0, f"{label} negative elapsed: {record}"
