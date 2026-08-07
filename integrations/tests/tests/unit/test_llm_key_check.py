"""Tests for `_check_llm_key` (idle-watcher.py) — the LLM_API_KEY verdict that
feeds the status line's `✕ (incorrect_llm_api_key)` glyph. The marker states
(`not_set`, `auth_failed`) are internal and unchanged; only the displayed label
collapses the two.

Regression this locks down: the probe used to demand a *successful* completion
before writing "ok", and `max_tokens=1` legitimately fails with a 400 on
reasoning models ("Could not finish the message because max_tokens or model
output limit was reached" — the one token goes to reasoning, leaving none for
content). Every check therefore came back `llm_key_check_inconclusive`, the
marker was never written, and a stale "not_set" from another session kept
accusing a perfectly good key forever.

The rule now: providers authenticate BEFORE validating anything else, so only
401/403 (or litellm's AuthenticationError) is a key verdict; any other HTTP
status proves the key was accepted; no status at all (timeout, DNS) is
inconclusive and must leave the marker alone.

cognee and litellm are faked via sys.modules — the watcher imports them lazily
inside the function, so no real venv, network, or provider call is involved.

Migrated from {claude-code,codex}/tests/test_llm_key_check.py.
"""

from __future__ import annotations

import json
import sys
import time
import types

import pytest


class AuthenticationError(Exception):
    """Stands in for litellm.AuthenticationError (matched by class name)."""

    status_code = 401


class ProviderError(Exception):
    """Any other litellm error; `status_code` is what the classifier reads."""

    def __init__(self, message="boom", status_code=None):
        super().__init__(message)
        self.status_code = status_code


class TransportError(Exception):
    """A local/network failure — carries no HTTP status at all."""


_COGNEE_MODULES = (
    "cognee",
    "cognee.infrastructure",
    "cognee.infrastructure.llm",
    "cognee.infrastructure.llm.config",
)

_SESSION = "my-session"


@pytest.fixture
def pc(suite, isolated_modules):
    return isolated_modules(suite, "_plugin_common")


@pytest.fixture
def run_check(suite, hook_module, isolated_modules, monkeypatch):
    """Run one `_check_llm_key` with cognee/litellm faked; return (writes, events).

    The watcher is loaded before _plugin_common so its function-local
    `from _plugin_common import ...` binds to the same module object the
    patches land on. `prior_session_key` is whose verdict is already in the
    marker — the throttle only honours THIS session's timestamp, so another
    session's fresh write must not stop us from validating our own key.
    """
    watcher = hook_module(suite, "idle-watcher.py")
    pc = isolated_modules(suite, "_plugin_common")

    def _run(
        raise_exc=None,
        key="sk-test",
        config=None,
        checked_at=0.0,
        env=None,
        prior_session_key=_SESSION,
    ):
        writes, events = [], []

        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)

        cfg = types.SimpleNamespace(
            llm_api_key=key,
            llm_model="openai/gpt-5-mini",
            llm_endpoint=None,
            llm_provider="openai",
        )
        for name in _COGNEE_MODULES:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        sys.modules["cognee.infrastructure.llm.config"].get_llm_config = lambda: cfg

        litellm = types.ModuleType("litellm")

        def _completion(**_kwargs):
            if raise_exc is not None:
                raise raise_exc
            return {"ok": True}

        litellm.completion = _completion
        monkeypatch.setitem(sys.modules, "litellm", litellm)

        monkeypatch.setattr(
            pc, "write_llm_state", lambda state, detail="": writes.append((state, detail))
        )
        monkeypatch.setattr(
            pc,
            "read_llm_state",
            lambda: {"checked_at": checked_at, "session_key": prior_session_key},
        )
        monkeypatch.setattr(pc, "get_session_key", lambda: _SESSION)
        monkeypatch.setattr(watcher, "_log", lambda event, **_kw: events.append(event))

        watcher._check_llm_key(config if config is not None else {})
        return writes, events

    return _run


# ── the regression: a non-auth provider rejection means the key WORKS ───────


def test_reasoning_model_output_limit_400_counts_as_ok(run_check):
    """`max_tokens=1` on a reasoning model 400s — that is proof of auth, not failure."""
    writes, events = run_check(
        raise_exc=ProviderError(
            "litellm.BadRequestError: OpenAIException - Could not finish the message "
            "because max_tokens or model output limit was reached.",
            status_code=400,
        )
    )
    assert writes == [("ok", "")], writes
    assert "llm_key_check_inconclusive" not in events, events


def test_model_not_found_404_counts_as_ok(run_check):
    writes, _ = run_check(raise_exc=ProviderError("no such model", status_code=404))
    assert writes == [("ok", "")], writes


def test_rate_limit_429_counts_as_ok(run_check):
    writes, _ = run_check(raise_exc=ProviderError("slow down", status_code=429))
    assert writes == [("ok", "")], writes


def test_provider_5xx_counts_as_ok(run_check):
    writes, _ = run_check(raise_exc=ProviderError("upstream boom", status_code=503))
    assert writes == [("ok", "")], writes


# ── genuine auth failures ──────────────────────────────────────────────────


def test_authentication_error_class_is_auth_failed(run_check):
    writes, events = run_check(raise_exc=AuthenticationError("invalid api key"))
    assert [state for state, _ in writes] == ["auth_failed"], writes
    assert writes[0][1].startswith("invalid api key")
    assert "llm_key_auth_failed" in events


def test_401_from_an_unnamed_error_class_is_auth_failed(run_check):
    """Gateways/proxies don't always surface litellm's exception type."""
    writes, _ = run_check(raise_exc=ProviderError("unauthorized", status_code=401))
    assert [state for state, _ in writes] == ["auth_failed"], writes


def test_403_is_auth_failed(run_check):
    writes, _ = run_check(raise_exc=ProviderError("forbidden", status_code=403))
    assert [state for state, _ in writes] == ["auth_failed"], writes


# ── genuinely inconclusive: never touch the marker ────────────────────────


def test_transport_failure_leaves_the_marker_untouched(run_check):
    writes, events = run_check(raise_exc=TransportError("connection reset"))
    assert writes == [], writes
    assert "llm_key_check_inconclusive" in events


def test_unparseable_status_is_inconclusive(run_check):
    writes, events = run_check(raise_exc=ProviderError("weird", status_code="not-a-number"))
    assert writes == [], writes
    assert "llm_key_check_inconclusive" in events


# ── the happy path and the no-key path ────────────────────────────────────


def test_successful_completion_is_ok(run_check):
    writes, events = run_check()
    assert writes == [("ok", "")], writes
    assert "llm_key_ok" in events


def test_missing_key_is_not_set(run_check):
    writes, events = run_check(key="")
    assert writes == [("not_set", "")], writes
    assert "llm_key_not_set" in events, "an unexplained ✕ (llm_no_key) must be traceable"


def test_blank_key_is_not_set(run_check):
    writes, _ = run_check(key="   ")
    assert writes == [("not_set", "")], writes


# ── gates: cloud mode, throttle, opt-out ──────────────────────────────────


def test_cloud_mode_is_skipped_entirely(run_check):
    writes, _ = run_check(config={"base_url": "https://api.cognee.ai"})
    assert writes == [], writes


def test_local_url_is_still_checked(run_check):
    writes, _ = run_check(config={"base_url": "http://localhost:8011"})
    assert writes == [("ok", "")], writes


def test_recent_check_by_this_session_is_throttled(run_check):
    writes, _ = run_check(checked_at=time.time(), env={"COGNEE_LLM_CHECK_INTERVAL": "300"})
    assert writes == [], writes


def test_recent_check_by_another_session_does_not_throttle_us(run_check):
    """The marker is machine-wide; another launch's verdict is not ours."""
    writes, _ = run_check(
        checked_at=time.time(),
        env={"COGNEE_LLM_CHECK_INTERVAL": "300"},
        prior_session_key="someone-else",
    )
    assert writes == [("ok", "")], writes


def test_opt_out_env_skips_the_check(run_check):
    writes, _ = run_check(env={"COGNEE_LLM_KEY_CHECK": "false"})
    assert writes == [], writes


# ── the marker carries who wrote it ───────────────────────────────────────


@pytest.fixture
def marker_env(pc, tmp_path, monkeypatch):
    """Redirect the LLM/connection markers into tmp_path; return pc."""
    monkeypatch.setattr(pc, "_LLM_STATE_MARKER", tmp_path / "llm-state.json")
    monkeypatch.setattr(pc, "_LLM_STATE_DIR", tmp_path / "llm-state")
    monkeypatch.setattr(pc, "_SERVER_READY_MARKER", tmp_path / "server-ready.json")
    monkeypatch.setattr(pc, "_CONN_STATE_DIR", tmp_path / "conn-state")
    return pc


def test_write_llm_state_stamps_the_session_key(marker_env, monkeypatch):
    """Without this stamp a keyless launch's verdict lands on every other bar."""
    pc = marker_env
    monkeypatch.setattr(pc, "get_session_key", lambda: "writing-session")
    pc.write_llm_state("not_set")
    written = json.loads(pc._LLM_STATE_MARKER.read_text(encoding="utf-8"))
    assert written["session_key"] == "writing-session", written
    assert written["llm_state"] == "not_set", written
    assert written["checked_at"] > 0, written


def test_write_llm_state_also_writes_the_per_session_copy(marker_env, monkeypatch):
    """The per-session file is what the bar reads; the shared one is for compat."""
    pc = marker_env
    monkeypatch.setattr(pc, "get_session_key", lambda: "writing-session")
    pc.write_llm_state("auth_failed", detail="nope")
    per = json.loads((pc._LLM_STATE_DIR / "writing-session.json").read_text(encoding="utf-8"))
    shared = json.loads(pc._LLM_STATE_MARKER.read_text(encoding="utf-8"))
    assert per == shared, (per, shared)
    assert per["llm_state"] == "auth_failed" and per["detail"] == "nope", per


def test_read_llm_state_prefers_this_sessions_record(marker_env, monkeypatch):
    """Otherwise the throttle reasons about whoever wrote the shared file last."""
    pc = marker_env
    monkeypatch.setattr(pc, "get_session_key", lambda: "mine")
    pc._LLM_STATE_MARKER.write_text(
        json.dumps({"llm_state": "ok", "session_key": "theirs"}), encoding="utf-8"
    )
    pc._LLM_STATE_DIR.mkdir(parents=True, exist_ok=True)
    (pc._LLM_STATE_DIR / "mine.json").write_text(
        json.dumps({"llm_state": "not_set", "session_key": "mine"}), encoding="utf-8"
    )
    got = pc.read_llm_state()
    assert got["llm_state"] == "not_set", got

    # No record of our own -> the shared file is the fallback.
    (pc._LLM_STATE_DIR / "mine.json").unlink()
    fallback = pc.read_llm_state()
    assert fallback["llm_state"] == "ok", fallback


def test_write_connection_state_stamps_and_mirrors(marker_env, monkeypatch):
    pc = marker_env
    monkeypatch.setattr(pc, "get_session_key", lambda: "writing-session")
    pc.write_connection_state("auth_failed", "http://localhost:8011")
    per = json.loads((pc._CONN_STATE_DIR / "writing-session.json").read_text(encoding="utf-8"))
    assert per["session_key"] == "writing-session", per
    assert per["state"] == "auth_failed", per


def test_session_marker_is_skipped_without_a_session_key(marker_env, monkeypatch):
    """An early bootstrap write has no key yet; the shared marker still lands."""
    pc = marker_env
    monkeypatch.setattr(pc, "get_session_key", lambda: "")
    pc.write_llm_state("ok")
    assert pc._LLM_STATE_MARKER.exists()
    assert not pc._LLM_STATE_DIR.exists()
