"""Tests for `_llm_prefix` / `_status_prefix` (cognee_statusline_render.py) — the
LLM-key verdict in the bar's single glyph slot.

Contract (shared by both suites):

  * only a FRESH verdict shows (`_LLM_STATE_STALE_SECONDS`); a missing,
    malformed, unstamped or unparseable marker renders nothing;
  * a verdict belongs to the session that wrote it — a keyless launch must not
    accuse every other terminal — while an unattributed (older) marker is still
    honoured rather than losing the signal;
  * both marker states (`not_set`, `auth_failed`) render as the same label,
    because the user's fix is the same either way;
  * one glyph slot, with precedence: a fresh server failure > an LLM-key failure
    > the ready dot. `●` and `✕` must never appear side by side;
  * cloud mode suppresses the glyph entirely — the local server's LLM key is not
    ours to report.

Parametrized over both suites; expected glyphs come from ``utils.statusline``.
The ANSI-reset rule is claude-only and the plain-text guard codex-only, as is
codex's ``render_status_for_host`` emitter (the string that reaches the model's
context). The full subprocess render lives in e2e/test_statusline_render.py.

Migrated from {claude-code,codex}/tests/test_statusline_llm_state.py.
"""

from __future__ import annotations

import time

import pytest
from utils.statusline import mode_label, ok_glyph, write_json

_LOCAL_URL = "http://127.0.0.1:8000"
_CLOUD_URL = "https://api.example-cognee.ai"
_MINE = "my-session"


@pytest.fixture
def sl(statusline, monkeypatch):
    """The renderer pinned to a local base_url (the LLM key only matters locally)."""
    monkeypatch.setenv("COGNEE_BASE_URL", _LOCAL_URL)
    return statusline


def _glyph_for(sl, reason: str) -> str:
    """The failure glyph for an already-user-facing reason label."""
    helper = getattr(sl, "_fail_glyph", None)
    return helper(reason) if helper else f"✕ ({reason}) "


def _llm_glyph(sl) -> str:
    """Both llm_state failures render this one label — the user's fix is the same."""
    return _glyph_for(sl, sl._LLM_KEY_REASON)


def _server_glyph(sl) -> str:
    return _glyph_for(sl, sl._COGNEE_KEY_REASON)


def _write_llm(sl, payload, *, session_key=None):
    """Write the LLM marker, stamping a fresh checked_at unless the test set one.

    Verdicts have a TTL, so an unstamped marker would read as stale and every
    glyph assertion would trivially pass on "".
    """
    if isinstance(payload, dict) and "llm_state" in payload:
        payload = {"checked_at": time.time(), **payload}
    path = sl._LLM_STATE_PATH if session_key is None else sl._LLM_STATE_DIR / f"{session_key}.json"
    if isinstance(payload, str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    else:
        write_json(path, payload)


def _write_server(sl, payload):
    write_json(sl._SERVER_READY_PATH, payload)


# ── no file / malformed / healthy → nothing shown ──────────────────────────


def test_no_marker_file_returns_empty(sl):
    assert sl._llm_prefix() == ""


def test_malformed_marker_returns_empty(sl):
    _write_llm(sl, "not json{{{")
    assert sl._llm_prefix() == ""


def test_marker_without_state_returns_empty(sl):
    _write_llm(sl, {"checked_at": time.time()})
    assert sl._llm_prefix() == ""


def test_state_ok_returns_empty(sl):
    _write_llm(sl, {"llm_state": "ok"})
    assert sl._llm_prefix() == ""


# ── verdicts expire: the marker is shared by every session ─────────────────


def test_stale_verdict_is_ignored(sl):
    """A verdict from a session that is long gone must stop accusing the key."""
    _write_llm(
        sl,
        {"llm_state": "not_set", "checked_at": time.time() - sl._LLM_STATE_STALE_SECONDS - 60},
    )
    assert sl._llm_prefix() == ""


def test_verdict_just_inside_the_window_still_shows(sl):
    _write_llm(
        sl,
        {"llm_state": "not_set", "checked_at": time.time() - sl._LLM_STATE_STALE_SECONDS + 60},
    )
    assert sl._llm_prefix() == _llm_glyph(sl)


def test_verdict_without_checked_at_is_ignored(sl):
    _write_llm(sl, {"llm_state": "auth_failed", "checked_at": None})
    assert sl._llm_prefix() == ""


def test_unparseable_checked_at_is_ignored(sl):
    _write_llm(sl, {"llm_state": "auth_failed", "checked_at": "yesterday"})
    assert sl._llm_prefix() == ""


# ── attribution: a keyless session must not accuse everyone else ───────────


def test_other_sessions_verdict_is_hidden(sl):
    """Observed for real: a launch without the export wrote not_set machine-wide."""
    _write_llm(sl, {"llm_state": "not_set", "session_key": "other-session"})
    assert sl._llm_prefix(_MINE) == ""
    assert sl._status_prefix(_MINE) == ""


def test_own_verdict_is_shown(sl):
    _write_llm(sl, {"llm_state": "not_set", "session_key": _MINE})
    assert sl._llm_prefix(_MINE) == _llm_glyph(sl)


def test_unattributed_verdict_is_still_shown(sl):
    """Older marker with no session_key — keep the signal rather than lose it."""
    _write_llm(sl, {"llm_state": "auth_failed"})
    assert sl._llm_prefix(_MINE) == _llm_glyph(sl)


def test_caller_without_a_session_id_sees_any_verdict(sl):
    _write_llm(sl, {"llm_state": "not_set", "session_key": "other-session"})
    assert sl._llm_prefix("") == _llm_glyph(sl)


# ── failures → a LEFT glyph, trailing space, no leading padding ────────────


def test_not_set_returns_leading_glyph(sl):
    _write_llm(sl, {"llm_state": "not_set"})
    prefix = sl._llm_prefix()
    assert prefix == _llm_glyph(sl)
    assert not prefix.startswith(" "), "must not carry the old trailing-segment padding"
    assert prefix.endswith(" "), "concatenates directly onto 'cognee: '"


def test_auth_failed_returns_leading_glyph(sl):
    _write_llm(sl, {"llm_state": "auth_failed"})
    assert sl._llm_prefix() == _llm_glyph(sl)


# ── cloud mode: the local server's LLM key is not ours to report ───────────


def test_cloud_mode_suppresses_llm_glyph(sl, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", _CLOUD_URL)
    _write_llm(sl, {"llm_state": "auth_failed"})
    assert sl._llm_prefix() == ""
    assert sl._status_prefix() == ""


# ── precedence inside the single slot ──────────────────────────────────────


def test_server_failure_wins_over_llm_failure(sl):
    # checked_at present and fresh: red is reserved for fresh failures (a failure
    # marker of unknown age renders no glyph since SDK-356).
    _write_llm(sl, {"llm_state": "not_set"})
    _write_server(sl, {"state": "auth_failed", "base_url": _LOCAL_URL, "checked_at": time.time()})
    assert sl._status_prefix() == _server_glyph(sl)


def test_llm_failure_replaces_the_ready_dot(sl):
    _write_llm(sl, {"llm_state": "not_set"})
    _write_server(sl, {"state": "ready", "base_url": _LOCAL_URL})
    prefix = sl._status_prefix()
    assert prefix == _llm_glyph(sl)
    assert "●" not in prefix, "● and ✕ side by side read as contradictory"


def test_ready_dot_survives_when_the_llm_key_is_fine(sl):
    _write_llm(sl, {"llm_state": "ok"})
    _write_server(sl, {"state": "ready", "base_url": _LOCAL_URL})
    assert sl._status_prefix() == ok_glyph(sl)


def test_llm_failure_shows_when_the_server_state_is_unknown(sl):
    _write_llm(sl, {"llm_state": "auth_failed"})  # no server marker
    assert sl._status_prefix() == _llm_glyph(sl)


# ── per-session verdicts: one terminal has the key, the other doesn't ──────


def test_own_verdict_file_is_preferred_over_the_shared_one(sl):
    _write_llm(sl, {"llm_state": "ok", "session_key": "other-session"})
    _write_llm(sl, {"llm_state": "not_set", "session_key": _MINE}, session_key=_MINE)
    assert sl._llm_prefix(_MINE) == _llm_glyph(sl)


def test_own_ok_is_not_reddened_by_another_sessions_not_set(sl):
    _write_llm(sl, {"llm_state": "not_set", "session_key": "other-session"})
    _write_llm(sl, {"llm_state": "ok", "session_key": _MINE}, session_key=_MINE)
    assert sl._llm_prefix(_MINE) == ""


def test_own_stale_verdict_still_expires(sl):
    _write_llm(
        sl,
        {
            "llm_state": "not_set",
            "session_key": _MINE,
            "checked_at": time.time() - sl._LLM_STATE_STALE_SECONDS - 60,
        },
        session_key=_MINE,
    )
    assert sl._llm_prefix(_MINE) == ""


# ── styling (claude-code) vs plain text (codex) ────────────────────────────


def test_colour_resets_before_the_separating_space(suite, sl):
    """A dangling ANSI sequence would tint the rest of the bar."""
    if not hasattr(sl, "_ok_glyph"):
        pytest.skip(f"{suite.name}: the bar is plain text by design (model context)")
    _write_llm(sl, {"llm_state": "not_set"})
    assert sl._llm_prefix().endswith("\033[0m ")


def test_no_ansi_escapes(suite, sl):
    if hasattr(sl, "_ok_glyph"):
        pytest.skip(f"{suite.name}: the bar is deliberately styled for a terminal")
    _write_llm(sl, {"llm_state": "not_set"})
    assert "\033" not in sl._llm_prefix()


def test_render_status_for_host_places_the_glyph_before_the_label(suite, sl, monkeypatch):
    """codex's hook-facing emitter — the string that reaches the model's context."""
    if not hasattr(sl, "render_status_for_host"):
        pytest.skip(f"{suite.name}: no render_status_for_host (the bar is terminal-only)")
    _write_llm(sl, {"llm_state": "not_set"})
    _write_server(sl, {"state": "ready", "base_url": _LOCAL_URL})
    out = sl.render_status_for_host("session-key")
    expected_mode = mode_label(sl, "local")
    assert out == f"{_llm_glyph(sl)}cognee: {suite.default_dataset} · {expected_mode}", repr(out)
