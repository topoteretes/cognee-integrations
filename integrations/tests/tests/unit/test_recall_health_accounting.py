"""The recall attempt IS the health probe (SDK-356).

Rather than pinging the server before each recall, the plugin folds every scope
call's outcome back into the shared connection state. That makes the status line
honest for free — but it also means the classifier has to distinguish failures
that look alike on the wire and mean very different things to the user:

  * a rejected key is doomed on every scope (one shared credential), so the rest
    of the budget must not be spent proving it four times;
  * a 5xx is per-request, so every scope still gets its turn;
  * a refused connection is definitive — unless nothing has ever answered here,
    which is a cold start, not an outage, and must stay silent;
  * a timeout says nothing on its own; only a streak of them earns a verdict, and
    never "unreachable" — the server exists, it just isn't answering;
  * a success is itself the ready probe, so it refreshes a stale marker and
    clears the breaker.

Contract, per branch: which state is written, whether the remaining scopes run,
and whether the breaker is fed. Timeouts must never feed the breaker — a slow
server tripping it would turn latency into a hard outage.

All registered suites carry these seams identically, so all are exercised.

Migrated from claude-code/tests/test_recall_health_accounting.py, which ran in no
CI job on any platform.
"""

from __future__ import annotations

import errno
import urllib.error

import pytest
from utils.recall import READY_PRIOR, URL, drive_recall


@pytest.fixture
def lookup(suite, hook_module):
    return hook_module(suite, "session-context-lookup.py")


def _raise(exc_factory):
    """A recall_via_http that always fails the same way."""

    def _fn(_prompt, **_kw):
        raise exc_factory()

    return _fn


def _http_error(code: int):
    return urllib.error.HTTPError(URL, code, "boom", {}, None)


def _refused():
    return urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))


# ── a rejected key: detected from the real request, budget not wasted ──────────


@pytest.mark.parametrize("code", [401, 403])
def test_a_rejected_key_is_recorded_and_stops_the_fan_out(lookup, monkeypatch, code):
    """Every scope shares one credential, so scope two would be refused too."""
    run = drive_recall(lookup, monkeypatch, recall=_raise(lambda: _http_error(code)))

    assert any(write[0] == "auth_failed" for write in run.writes), run.writes
    assert len(run.calls) == 1, f"remaining scopes must be skipped, got {run.calls}"
    assert run.fired("recall_auth_rejected"), run.events


def test_the_auth_verdict_names_the_statuses_that_caused_it(lookup, monkeypatch):
    """The written detail is what a user reads in `doctor`; keep it specific."""
    run = drive_recall(lookup, monkeypatch, recall=_raise(lambda: _http_error(401)))
    assert ("auth_failed", URL, "401/403 during recall") in run.writes, run.writes


# ── 5xx: reachable but failing ────────────────────────────────────────────────


def test_a_failing_server_is_recorded_once_and_every_scope_still_tries(lookup, monkeypatch):
    """A 5xx is per-request, not a shared-key verdict — no reason to give up."""
    run = drive_recall(lookup, monkeypatch, recall=_raise(lambda: _http_error(503)))

    assert ("server_error", URL, "5xx during recall") in run.writes, run.writes
    assert run.breaker == [("failure", URL, "server_error")], (
        f"exactly one breaker failure per prompt, not per scope: {run.breaker}"
    )
    assert len(run.calls) == 4, f"all four scopes should still run: {run.calls}"


# ── refused: definitive down, but silent on a cold start ──────────────────────


def test_a_refused_connection_is_an_outage_once_the_server_has_answered_before(lookup, monkeypatch):
    run = drive_recall(lookup, monkeypatch, recall=_raise(_refused), prior_state=READY_PRIOR)

    assert any(write[0] == "unreachable" for write in run.writes), run.writes
    assert ("failure", URL, "unreachable") in run.breaker, run.breaker
    assert len(run.calls) == 1, f"remaining scopes must be skipped, got {run.calls}"


def test_a_refused_connection_with_no_history_is_warming_and_says_nothing(lookup, monkeypatch):
    """The plugin boots its own server; complaining during startup is noise."""
    run = drive_recall(lookup, monkeypatch, recall=_raise(_refused), prior_state=None)

    assert run.writes == [], f"a cold start must write no verdict: {run.writes}"
    assert run.breaker == [], f"a cold start must not feed the breaker: {run.breaker}"


# ── timeouts: no verdict until the streak threshold ───────────────────────────


def test_a_single_slow_prompt_earns_no_verdict(lookup, monkeypatch):
    run = drive_recall(
        lookup,
        monkeypatch,
        recall=_raise(lambda: TimeoutError("timed out")),
        slow_streak=1,
        slow_threshold=3,
    )

    assert run.writes == [], f"one slow prompt is not a diagnosis: {run.writes}"
    assert run.breaker == [], "timeouts must never feed the breaker"
    assert len(run.calls) == 4, f"a timeout is not a shared verdict: {run.calls}"


def test_a_streak_of_slow_prompts_escalates_but_never_to_unreachable(lookup, monkeypatch):
    """Nothing was refused, so the server is there — just not answering."""
    run = drive_recall(
        lookup,
        monkeypatch,
        recall=_raise(lambda: TimeoutError("timed out")),
        slow_streak=3,
        slow_threshold=3,
    )

    assert any(write[0] == "not_responding" for write in run.writes), run.writes
    assert not any(write[0] == "unreachable" for write in run.writes), (
        f"a slow server must not be reported as down: {run.writes}"
    )
    assert run.breaker == [], "timeouts must not feed the breaker even at the threshold"
    assert run.fired("slow_streak_escalated"), run.events


# ── success: the attempt is the ready probe ───────────────────────────────────


def test_a_successful_recall_refreshes_a_stale_marker_and_clears_the_breaker(lookup, monkeypatch):
    run = drive_recall(lookup, monkeypatch, recall={}, ready_hint=False)

    assert ("ready", URL, "") in run.writes, run.writes
    assert ("success", URL) in run.breaker, run.breaker


def test_a_successful_recall_with_a_fresh_marker_writes_nothing(lookup, monkeypatch):
    """Rewriting a marker that is already fresh is pure disk churn per prompt."""
    run = drive_recall(lookup, monkeypatch, recall={}, ready_hint=True)

    assert run.writes == [], run.writes
