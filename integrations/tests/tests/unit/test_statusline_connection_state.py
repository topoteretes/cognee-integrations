"""Tests for `_health_prefix` / `_connection_marker` (cognee_statusline_render.py) —
the server-connection glyph, resolved PER TERMINAL.

Two sessions can legitimately disagree about the same server: they may hold
different `COGNEE_API_KEY`s, so one gets `auth_failed` while the other is `ready`.
The shared `server-ready.json` has a single writer-wins slot, so each session also
writes `conn-state/<session_key>.json` and the bar reads that first. Resolution
order under test:

  1. our own record wins…
  2. …except when the shared marker holds a FRESHER failure — the server is shared,
     so a just-observed outage applies to every terminal;
  3. with no record of our own, the shared marker is used only when unattributed;
     a record belonging to another session is ignored (no glyph, like warming);
  4. the recall breaker still overrides a "ready", and a base_url mismatch is still
     ignored.

Parametrized over all registered suites: the resolution logic is shared, and the
expected glyphs come from ``utils.statusline`` (Claude Code styles them with ANSI;
Codex and Antigravity keep them plain — see the colour-policy section at the
bottom and Codex's test_statusline_plain_text).

Marker paths derive from Path.home(), so the isolated import already points them
inside the per-test HOME. Migrated from
{claude-code,codex}/tests/test_statusline_connection_state.py.
"""

from __future__ import annotations

import itertools
import json
import pathlib
import time

import pytest
from utils.statusline import fail_glyph, ok_glyph, write_json

_URL = "http://127.0.0.1:8000"
_MINE = "my-session"
_THEIRS = "their-session"


@pytest.fixture
def sl(statusline, monkeypatch):
    """The renderer with the active base_url pinned to _URL (the common case)."""
    monkeypatch.setenv("COGNEE_BASE_URL", _URL)
    return statusline


def _rec(state, session_key=None, age=0.0, base_url=_URL):
    """A connection record `age` seconds old."""
    now = time.time() - age
    rec = {
        "state": state,
        "base_url": base_url,
        "checked_at": now,
        "ready_at": now if state == "ready" else 0,
    }
    if session_key is not None:
        rec["session_key"] = session_key
    return rec


def _markers(sl, *, shared=None, mine=None, breaker=None):
    """Write the marker files a test needs (all inside the temp HOME)."""
    if shared is not None:
        write_json(sl._SERVER_READY_PATH, shared)
    if mine is not None:
        write_json(sl._CONN_STATE_DIR / f"{_MINE}.json", mine)
    if breaker is not None:
        write_json(sl._BREAKER_PATH, breaker)


def _breaker_for(url, *, cooldown_delta=60, reason="unreachable"):
    """A per-server breaker file (SDK-356 schema) for `url`."""
    return {"servers": {url: {"cooldown_until": time.time() + cooldown_delta, "reason": reason}}}


# ── our own record is what this terminal shows ─────────────────────────────


def test_own_ready_record_renders_the_dot(sl):
    _markers(sl, mine=_rec("ready", _MINE))
    assert sl._health_prefix(_MINE) == ok_glyph(sl)


def test_own_failure_record_renders_its_reason(sl):
    _markers(sl, mine=_rec("auth_failed", _MINE))
    assert sl._health_prefix(_MINE) == fail_glyph(sl, "auth_failed")


def test_two_terminals_can_disagree(sl):
    """The point of the whole exercise: their auth_failed is not our verdict."""
    _markers(sl, shared=_rec("auth_failed", _THEIRS), mine=_rec("ready", _MINE))
    assert sl._health_prefix(_MINE) == ok_glyph(sl)


# ── falling back to the shared marker ─────────────────────────────────────


def test_unattributed_shared_marker_is_used(sl):
    """Pre-upgrade writer, or a write made before the session key was known."""
    _markers(sl, shared=_rec("ready"))
    assert sl._health_prefix(_MINE) == ok_glyph(sl)


def test_legacy_marker_without_state_reads_as_ready(sl):
    _markers(sl, shared={"ready_at": time.time(), "base_url": _URL})
    assert sl._health_prefix(_MINE) == ok_glyph(sl)


def test_another_sessions_marker_is_ignored_when_we_have_none(sl):
    _markers(sl, shared=_rec("auth_failed", _THEIRS))
    assert sl._health_prefix(_MINE) == ""


def test_another_sessions_ready_does_not_green_us(sl):
    _markers(sl, shared=_rec("ready", _THEIRS))
    assert sl._health_prefix(_MINE) == ""


def test_caller_without_a_session_id_uses_the_shared_marker(sl):
    _markers(sl, shared=_rec("ready", _THEIRS))
    assert sl._health_prefix("") == ok_glyph(sl)


# ── a fresher shared FAILURE wins: the server is shared ───────────────────


def test_fresher_shared_failure_overrides_our_older_ready(sl):
    _markers(sl, shared=_rec("unreachable", _THEIRS, age=1), mine=_rec("ready", _MINE, age=600))
    assert sl._health_prefix(_MINE) == fail_glyph(sl, "unreachable")


def test_our_fresher_ready_beats_a_stale_shared_failure(sl):
    _markers(sl, shared=_rec("unreachable", _THEIRS, age=600), mine=_rec("ready", _MINE, age=1))
    assert sl._health_prefix(_MINE) == ok_glyph(sl)


def test_a_fresher_shared_ready_does_not_clear_our_failure(sl):
    """Only failures cross session boundaries — their working key is not ours."""
    _markers(sl, shared=_rec("ready", _THEIRS, age=1), mine=_rec("auth_failed", _MINE, age=600))
    assert sl._health_prefix(_MINE) == fail_glyph(sl, "auth_failed")


# ── the pre-existing rules still hold ────────────────────────────────────


def test_open_breaker_overrides_our_ready(sl):
    _markers(sl, mine=_rec("ready", _MINE), breaker=_breaker_for(_URL))
    assert sl._health_prefix(_MINE) == fail_glyph(sl, "unreachable")


def test_expired_breaker_leaves_ready_alone(sl):
    _markers(sl, mine=_rec("ready", _MINE), breaker=_breaker_for(_URL, cooldown_delta=-60))
    assert sl._health_prefix(_MINE) == ok_glyph(sl)


def test_breaker_reports_its_real_trip_reason(sl):
    """A breaker opened by 5xx must read server_error, not a false unreachable."""
    _markers(sl, mine=_rec("ready", _MINE), breaker=_breaker_for(_URL, reason="server_error"))
    assert sl._health_prefix(_MINE) == fail_glyph(sl, "server_error")


def test_another_servers_breaker_does_not_red_this_bar(sl):
    """Cloud failures (or the other suite's target) must not red a local terminal."""
    _markers(
        sl,
        mine=_rec("ready", _MINE),
        breaker=_breaker_for("https://tenant-x.aws.cognee.ai"),
    )
    assert sl._health_prefix(_MINE) == ok_glyph(sl)


def test_legacy_flat_breaker_file_is_ignored(sl):
    """The pre-upgrade machine-wide breaker was target-blind — never render it."""
    _markers(
        sl,
        mine=_rec("ready", _MINE),
        breaker={"failures": 99, "cooldown_until": time.time() + 3600},
    )
    assert sl._health_prefix(_MINE) == ok_glyph(sl)


# ── SDK-356: honest, fresh, definitive ─────────────────────────────────────


def test_not_responding_renders_its_own_reason(sl):
    """The escalated timeout streak is its own state — never "unreachable"."""
    _markers(sl, mine=_rec("not_responding", _MINE))
    out = sl._health_prefix(_MINE)
    assert out == fail_glyph(sl, "not_responding"), repr(out)
    assert "unreachable" not in out


def test_fresher_shared_not_responding_overrides_our_older_ready(sl):
    """Server-wide: a server that isn't answering isn't answering anyone."""
    _markers(sl, shared=_rec("not_responding", _THEIRS, age=1), mine=_rec("ready", _MINE, age=600))
    assert sl._health_prefix(_MINE) == fail_glyph(sl, "not_responding")


def test_stale_failure_marker_renders_no_glyph(sl):
    """A failure nobody has re-confirmed within the TTL is ambiguity, not red."""
    _markers(sl, mine=_rec("unreachable", _MINE, age=sl._FAIL_STATE_STALE_SECONDS + 60))
    assert sl._health_prefix(_MINE) == ""


def test_fresh_failure_marker_still_renders(sl):
    _markers(sl, mine=_rec("unreachable", _MINE, age=60))
    assert sl._health_prefix(_MINE) == fail_glyph(sl, "unreachable")


def test_base_url_mismatch_is_ignored(sl):
    """A local-ready record must never green a cloud session."""
    _markers(sl, mine=_rec("ready", _MINE, base_url="https://other.example.com"))
    assert sl._health_prefix(_MINE) == ""


def test_no_markers_at_all_renders_no_glyph(sl):
    assert sl._health_prefix(_MINE) == ""


def test_path_unsafe_session_id_is_not_used_as_a_path(sl):
    _markers(sl, shared=_rec("ready"))
    assert sl._health_prefix("../../etc/passwd") == ok_glyph(sl)


# ── only server-wide failures cross session boundaries ────────────────────


def test_another_sessions_auth_failure_does_not_leak_into_our_bar(statusline, monkeypatch):
    """The regression: a keyless CLOUD terminal starting up must not red a healthy
    LOCAL one. `auth_failed` describes that session's credential, not the server."""
    sl = statusline  # nothing exported — the default local setup
    _markers(
        sl,
        shared=_rec("auth_failed", _THEIRS, age=1, base_url="https://tenant-x.aws.cognee.ai"),
        mine=_rec("ready", _MINE, age=30, base_url=sl._DEFAULT_LOCAL_BASE_URL),
    )
    assert sl._health_prefix(_MINE) == ok_glyph(sl)


def test_another_sessions_auth_failure_on_our_own_target_does_not_leak_either(sl):
    """Even on the same URL: their key being rejected says nothing about ours."""
    _markers(sl, shared=_rec("auth_failed", _THEIRS, age=1), mine=_rec("ready", _MINE, age=30))
    assert sl._health_prefix(_MINE) == ok_glyph(sl)


def test_fresher_shared_server_error_still_overrides(sl):
    _markers(sl, shared=_rec("server_error", _THEIRS, age=1), mine=_rec("ready", _MINE, age=600))
    assert sl._health_prefix(_MINE) == fail_glyph(sl, "server_error")


# ── the mismatch guard has a URL to compare against ───────────────────────


def test_active_base_url_defaults_to_localhost(statusline):
    """Without this the guard is toothless: no URL of our own means no mismatch."""
    assert statusline._active_base_url() == statusline._DEFAULT_LOCAL_BASE_URL


def test_active_base_url_honours_the_local_api_url_var_first(sl, monkeypatch):
    """Mirrors _plugin_common._local_api_url_with_source, which stamps the markers."""
    monkeypatch.setenv("COGNEE_LOCAL_API_URL", "http://127.0.0.1:9999/")
    assert sl._active_base_url() == "http://127.0.0.1:9999"


def test_a_cloud_marker_is_rejected_by_a_default_local_session(statusline):
    """No record of our own, and the shared one is for a different server."""
    _markers(
        statusline,
        shared=_rec("unreachable", None, age=1, base_url="https://tenant-x.aws.cognee.ai"),
    )
    assert statusline._health_prefix(_MINE) == ""


def test_our_own_localhost_record_still_matches_the_default(statusline):
    _markers(
        statusline,
        mine=_rec("ready", _MINE, base_url=statusline._DEFAULT_LOCAL_BASE_URL),
    )
    assert statusline._health_prefix(_MINE) == ok_glyph(statusline)


# ── containment: an accepted session id can never leave its directory ─────


def test_path_safe_rejects_every_separator(sl):
    """`.` is allowed, so `..` passes — harmless, because no SEPARATOR ever does."""
    for bad in (
        "../../etc",
        "..././secrets",
        "../../../etc/passwd",
        "a/b",
        "..\\..\\win",
        "C:",
        "sess:stream",
        "\0evil",
        "",
    ):
        assert not sl._path_safe(bad), bad


def test_accepted_ids_always_stay_inside_the_directory(sl, tmp_path):
    """Exhaustive over an adversarial alphabet: no accepted id escapes."""
    root = tmp_path / "path-guard"
    for n in (1, 2, 3):
        for tup in itertools.product("a1._-/\\:~\0 ", repeat=n):
            sid = "".join(tup)
            if not sl._path_safe(sid):
                continue
            assert (root / f"{sid}.json").resolve().parent == root.resolve(), sid


def test_dotdot_becomes_a_filename_not_a_parent_hop(sl, tmp_path):
    root = tmp_path / "path-guard"
    assert sl._path_safe("..")
    assert (root / "...json").resolve() == (root / "...json").resolve()
    assert (root / "...json").resolve().parent == root.resolve()


# ── colour policy (claude-code only: codex's bar must stay plain text) ─────


@pytest.fixture
def styled(suite, sl):
    if not hasattr(sl, "_ok_glyph"):
        pytest.skip(f"{suite.name}: the bar is plain text by design (model context)")
    return sl


def test_healthy_dot_is_green(styled):
    _markers(styled, mine=_rec("ready", _MINE))
    out = styled._health_prefix(_MINE)
    assert out.startswith("\033[1;32m"), repr(out)  # bold green
    assert out.endswith("\033[0m "), repr(out)
    assert "●" in out


def test_failure_and_its_reason_are_both_red(styled):
    """The reason travels inside the red, so the verdict reads as one unit."""
    _markers(styled, mine=_rec("auth_failed", _MINE))
    out = styled._health_prefix(_MINE)
    assert out.startswith("\033[1;31m"), repr(out)  # bold red
    plain = out.replace("\033[1;31m", "").replace("\033[0m", "")
    assert f"✕ ({styled._COGNEE_KEY_REASON})" in plain, plain
    assert out.index("\033[0m") > out.index(styled._COGNEE_KEY_REASON), (
        "reason must be inside the red"
    )


def test_failure_is_detected_despite_the_colour_prefix(styled):
    """_status_prefix must not use startswith('✕') — an escape now precedes it."""
    _markers(styled, mine=_rec("unreachable", _MINE))
    assert styled._status_prefix(_MINE) == fail_glyph(styled, "unreachable")


# ── plain-text guard (codex only: the bar goes into the model's context) ───


@pytest.fixture
def plain(suite, sl):
    if hasattr(sl, "_ok_glyph"):
        pytest.skip(f"{suite.name}: the bar is deliberately styled for a terminal")
    return sl


def test_no_ansi_escapes_in_any_connection_glyph(plain):
    for state in ("ready", "auth_failed", "unreachable", "not_responding", "server_error"):
        write_json(plain._CONN_STATE_DIR / f"{_MINE}.json", _rec(state, _MINE))
        out = plain._health_prefix(_MINE)
        assert "\033" not in out, (state, repr(out))


def test_status_prefix_stays_plain_for_a_failure(plain):
    _markers(plain, mine=_rec("unreachable", _MINE))
    out = plain._status_prefix(_MINE)
    assert "\033" not in out, repr(out)
    assert out == fail_glyph(plain, "unreachable")


def test_marker_files_are_json_not_ansi(plain):
    """Sanity: the state on disk is data; only the render step formats it."""
    path = write_json(plain._CONN_STATE_DIR / f"{_MINE}.json", _rec("ready", _MINE))
    assert json.loads(pathlib.Path(path).read_text(encoding="utf-8"))["state"] == "ready"
