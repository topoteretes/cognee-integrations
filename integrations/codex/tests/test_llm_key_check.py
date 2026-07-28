"""Tests for `_check_llm_key` (idle-watcher.py, Codex) — the LLM_API_KEY verdict that
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

Run: python integrations/codex/tests/test_llm_key_check.py (or via pytest).
"""

import importlib.util
import os
import pathlib
import sys
import types

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _plugin_common  # noqa: E402


def _load_watcher():
    """Import idle-watcher.py under a module name (the filename has a hyphen)."""
    spec = importlib.util.spec_from_file_location(
        "idle_watcher_under_test", _SCRIPTS / "idle-watcher.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_WATCHER = _load_watcher()


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


def _run(
    raise_exc=None,
    key="sk-test",
    config=None,
    checked_at=0.0,
    env=None,
    prior_session_key=_SESSION,
):
    """Run one `_check_llm_key` with cognee/litellm faked.

    `prior_session_key` is whose verdict is already in the marker — the throttle
    only honours THIS session's timestamp, so another session's fresh write must
    not stop us from validating our own key.

    Returns ``(writes, events)``: writes is a list of ``(state, detail)`` the
    watcher recorded (empty = marker left untouched), events the `_log` names.
    """
    writes, events = [], []

    saved_modules = {name: sys.modules.get(name) for name in _COGNEE_MODULES + ("litellm",)}
    saved_env = {
        k: os.environ.get(k) for k in ("COGNEE_LLM_KEY_CHECK", "COGNEE_LLM_CHECK_INTERVAL")
    }
    saved_common = (
        _plugin_common.write_llm_state,
        _plugin_common.read_llm_state,
        _plugin_common.get_session_key,
    )
    saved_log = _WATCHER._log

    try:
        for k, v in (env or {}).items():
            os.environ[k] = v
        for k in saved_env:
            if k not in (env or {}) and k in os.environ:
                del os.environ[k]

        cfg = types.SimpleNamespace(
            llm_api_key=key,
            llm_model="openai/gpt-5-mini",
            llm_endpoint=None,
            llm_provider="openai",
        )
        for name in _COGNEE_MODULES:
            sys.modules[name] = types.ModuleType(name)
        sys.modules["cognee.infrastructure.llm.config"].get_llm_config = lambda: cfg

        litellm = types.ModuleType("litellm")

        def _completion(**_kwargs):
            if raise_exc is not None:
                raise raise_exc
            return {"ok": True}

        litellm.completion = _completion
        sys.modules["litellm"] = litellm

        _plugin_common.write_llm_state = lambda state, detail="": writes.append((state, detail))
        _plugin_common.read_llm_state = lambda: {
            "checked_at": checked_at,
            "session_key": prior_session_key,
        }
        _plugin_common.get_session_key = lambda: _SESSION
        _WATCHER._log = lambda event, **_kw: events.append(event)

        _WATCHER._check_llm_key(config if config is not None else {})
    finally:
        _WATCHER._log = saved_log
        (
            _plugin_common.write_llm_state,
            _plugin_common.read_llm_state,
            _plugin_common.get_session_key,
        ) = saved_common
        for name, mod in saved_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    return writes, events


# ── the regression: a non-auth provider rejection means the key WORKS ───────


def test_reasoning_model_output_limit_400_counts_as_ok():
    """`max_tokens=1` on a reasoning model 400s — that is proof of auth, not failure."""
    writes, events = _run(
        raise_exc=ProviderError(
            "litellm.BadRequestError: OpenAIException - Could not finish the message "
            "because max_tokens or model output limit was reached.",
            status_code=400,
        )
    )
    assert writes == [("ok", "")], writes
    assert "llm_key_check_inconclusive" not in events, events


def test_model_not_found_404_counts_as_ok():
    writes, _ = _run(raise_exc=ProviderError("no such model", status_code=404))
    assert writes == [("ok", "")], writes


def test_rate_limit_429_counts_as_ok():
    writes, _ = _run(raise_exc=ProviderError("slow down", status_code=429))
    assert writes == [("ok", "")], writes


def test_provider_5xx_counts_as_ok():
    writes, _ = _run(raise_exc=ProviderError("upstream boom", status_code=503))
    assert writes == [("ok", "")], writes


# ── genuine auth failures ──────────────────────────────────────────────────


def test_authentication_error_class_is_auth_failed():
    writes, events = _run(raise_exc=AuthenticationError("invalid api key"))
    assert [state for state, _ in writes] == ["auth_failed"], writes
    assert writes[0][1].startswith("invalid api key")
    assert "llm_key_auth_failed" in events


def test_401_from_an_unnamed_error_class_is_auth_failed():
    """Gateways/proxies don't always surface litellm's exception type."""
    writes, _ = _run(raise_exc=ProviderError("unauthorized", status_code=401))
    assert [state for state, _ in writes] == ["auth_failed"], writes


def test_403_is_auth_failed():
    writes, _ = _run(raise_exc=ProviderError("forbidden", status_code=403))
    assert [state for state, _ in writes] == ["auth_failed"], writes


# ── genuinely inconclusive: never touch the marker ────────────────────────


def test_transport_failure_leaves_the_marker_untouched():
    writes, events = _run(raise_exc=TransportError("connection reset"))
    assert writes == [], writes
    assert "llm_key_check_inconclusive" in events


def test_unparseable_status_is_inconclusive():
    writes, events = _run(raise_exc=ProviderError("weird", status_code="not-a-number"))
    assert writes == [], writes
    assert "llm_key_check_inconclusive" in events


# ── the happy path and the no-key path ────────────────────────────────────


def test_successful_completion_is_ok():
    writes, events = _run()
    assert writes == [("ok", "")], writes
    assert "llm_key_ok" in events


def test_missing_key_is_not_set():
    writes, events = _run(key="")
    assert writes == [("not_set", "")], writes
    assert "llm_key_not_set" in events, "an unexplained ✕ (llm_no_key) must be traceable"


def test_blank_key_is_not_set():
    writes, _ = _run(key="   ")
    assert writes == [("not_set", "")], writes


# ── gates: cloud mode, throttle, opt-out ──────────────────────────────────


def test_cloud_mode_is_skipped_entirely():
    writes, _ = _run(config={"base_url": "https://api.cognee.ai"})
    assert writes == [], writes


def test_local_url_is_still_checked():
    writes, _ = _run(config={"base_url": "http://localhost:8011"})
    assert writes == [("ok", "")], writes


def test_recent_check_by_this_session_is_throttled():
    import time

    writes, _ = _run(checked_at=time.time(), env={"COGNEE_LLM_CHECK_INTERVAL": "300"})
    assert writes == [], writes


def test_recent_check_by_another_session_does_not_throttle_us():
    """The marker is machine-wide; another launch's verdict is not ours."""
    import time

    writes, _ = _run(
        checked_at=time.time(),
        env={"COGNEE_LLM_CHECK_INTERVAL": "300"},
        prior_session_key="someone-else",
    )
    assert writes == [("ok", "")], writes


def test_opt_out_env_skips_the_check():
    writes, _ = _run(env={"COGNEE_LLM_KEY_CHECK": "false"})
    assert writes == [], writes


# ── the marker carries who wrote it ───────────────────────────────────────


def test_write_llm_state_stamps_the_session_key():
    """Without this stamp a keyless launch's verdict lands on every other bar."""
    import json
    import shutil
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp())
    orig_marker = _plugin_common._LLM_STATE_MARKER
    orig_key = _plugin_common.get_session_key
    try:
        _plugin_common._LLM_STATE_MARKER = tmp / "llm-state.json"
        _plugin_common.get_session_key = lambda: "writing-session"
        _plugin_common.write_llm_state("not_set")
        written = json.loads(_plugin_common._LLM_STATE_MARKER.read_text(encoding="utf-8"))
    finally:
        _plugin_common._LLM_STATE_MARKER = orig_marker
        _plugin_common.get_session_key = orig_key
        shutil.rmtree(tmp, ignore_errors=True)

    assert written["session_key"] == "writing-session", written
    assert written["llm_state"] == "not_set", written
    assert written["checked_at"] > 0, written


def test_write_llm_state_also_writes_the_per_session_copy():
    """The per-session file is what the bar reads; the shared one is for compat."""
    import json
    import shutil
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp())
    orig = (
        _plugin_common._LLM_STATE_MARKER,
        _plugin_common._LLM_STATE_DIR,
        _plugin_common.get_session_key,
    )
    try:
        _plugin_common._LLM_STATE_MARKER = tmp / "llm-state.json"
        _plugin_common._LLM_STATE_DIR = tmp / "llm-state"
        _plugin_common.get_session_key = lambda: "writing-session"
        _plugin_common.write_llm_state("auth_failed", detail="nope")
        per = json.loads(
            (_plugin_common._LLM_STATE_DIR / "writing-session.json").read_text(encoding="utf-8")
        )
        shared = json.loads(_plugin_common._LLM_STATE_MARKER.read_text(encoding="utf-8"))
    finally:
        (
            _plugin_common._LLM_STATE_MARKER,
            _plugin_common._LLM_STATE_DIR,
            _plugin_common.get_session_key,
        ) = orig
        shutil.rmtree(tmp, ignore_errors=True)

    assert per == shared, (per, shared)
    assert per["llm_state"] == "auth_failed" and per["detail"] == "nope", per


def test_read_llm_state_prefers_this_sessions_record():
    """Otherwise the throttle reasons about whoever wrote the shared file last."""
    import json
    import shutil
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp())
    orig = (
        _plugin_common._LLM_STATE_MARKER,
        _plugin_common._LLM_STATE_DIR,
        _plugin_common.get_session_key,
    )
    try:
        _plugin_common._LLM_STATE_MARKER = tmp / "llm-state.json"
        _plugin_common._LLM_STATE_DIR = tmp / "llm-state"
        _plugin_common.get_session_key = lambda: "mine"
        _plugin_common._LLM_STATE_MARKER.write_text(
            json.dumps({"llm_state": "ok", "session_key": "theirs"}), encoding="utf-8"
        )
        _plugin_common._LLM_STATE_DIR.mkdir(parents=True, exist_ok=True)
        (_plugin_common._LLM_STATE_DIR / "mine.json").write_text(
            json.dumps({"llm_state": "not_set", "session_key": "mine"}), encoding="utf-8"
        )
        got = _plugin_common.read_llm_state()

        # No record of our own -> the shared file is the fallback.
        (_plugin_common._LLM_STATE_DIR / "mine.json").unlink()
        fallback = _plugin_common.read_llm_state()
    finally:
        (
            _plugin_common._LLM_STATE_MARKER,
            _plugin_common._LLM_STATE_DIR,
            _plugin_common.get_session_key,
        ) = orig
        shutil.rmtree(tmp, ignore_errors=True)

    assert got["llm_state"] == "not_set", got
    assert fallback["llm_state"] == "ok", fallback


def test_write_connection_state_stamps_and_mirrors():
    import json
    import shutil
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp())
    orig = (
        _plugin_common._SERVER_READY_MARKER,
        _plugin_common._CONN_STATE_DIR,
        _plugin_common.get_session_key,
    )
    try:
        _plugin_common._SERVER_READY_MARKER = tmp / "server-ready.json"
        _plugin_common._CONN_STATE_DIR = tmp / "conn-state"
        _plugin_common.get_session_key = lambda: "writing-session"
        _plugin_common.write_connection_state("auth_failed", "http://localhost:8011")
        per = json.loads(
            (_plugin_common._CONN_STATE_DIR / "writing-session.json").read_text(encoding="utf-8")
        )
    finally:
        (
            _plugin_common._SERVER_READY_MARKER,
            _plugin_common._CONN_STATE_DIR,
            _plugin_common.get_session_key,
        ) = orig
        shutil.rmtree(tmp, ignore_errors=True)

    assert per["session_key"] == "writing-session", per
    assert per["state"] == "auth_failed", per


def test_session_marker_is_skipped_without_a_session_key():
    """An early bootstrap write has no key yet; the shared marker still lands."""
    import shutil
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp())
    orig = (
        _plugin_common._LLM_STATE_MARKER,
        _plugin_common._LLM_STATE_DIR,
        _plugin_common.get_session_key,
    )
    try:
        _plugin_common._LLM_STATE_MARKER = tmp / "llm-state.json"
        _plugin_common._LLM_STATE_DIR = tmp / "llm-state"
        _plugin_common.get_session_key = lambda: ""
        _plugin_common.write_llm_state("ok")
        assert _plugin_common._LLM_STATE_MARKER.exists()
        assert not _plugin_common._LLM_STATE_DIR.exists()
    finally:
        (
            _plugin_common._LLM_STATE_MARKER,
            _plugin_common._LLM_STATE_DIR,
            _plugin_common.get_session_key,
        ) = orig
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {_name}: {exc}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
