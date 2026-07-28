"""Tests for `_health_prefix` / `_connection_marker` (cognee_statusline_render.py) —
the server-connection glyph, resolved PER TERMINAL (Codex).

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

No unittest.mock: module-level marker paths are reassigned to a tmp dir and
restored in `finally`.

Run: python integrations/codex/tests/test_statusline_connection_state.py
(or via pytest).
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import cognee_statusline_render as sl  # noqa: E402

_URL = "http://127.0.0.1:8000"
_MINE = "my-session"
_THEIRS = "their-session"
_READY = "● "


class _Markers:
    """Redirect the connection markers at a tmp dir and pin the active base_url."""

    def __init__(self, shared=None, mine=None, breaker=None, base_url=_URL):
        self._shared = shared
        self._mine = mine
        self._breaker = breaker
        # base_url=None leaves COGNEE_BASE_URL unset — the default local setup, where
        # the renderer falls back to its localhost default.
        self._base_url = base_url

    def __enter__(self):
        self._dir = pathlib.Path(tempfile.mkdtemp())
        self._orig = (
            sl._SERVER_READY_PATH,
            sl._CONN_STATE_DIR,
            sl._BREAKER_PATH,
            sl._CONFIG_PATH,
        )
        self._orig_env = {k: os.environ.get(k) for k in ("COGNEE_BASE_URL", "COGNEE_LOCAL_API_URL")}

        sl._SERVER_READY_PATH = self._dir / "server-ready.json"
        sl._CONN_STATE_DIR = self._dir / "conn-state"
        sl._BREAKER_PATH = self._dir / "recall-breaker.json"
        sl._CONFIG_PATH = self._dir / "config.json"  # never written
        os.environ.pop("COGNEE_LOCAL_API_URL", None)
        if self._base_url is None:
            os.environ.pop("COGNEE_BASE_URL", None)
        else:
            os.environ["COGNEE_BASE_URL"] = self._base_url

        if self._shared is not None:
            sl._SERVER_READY_PATH.write_text(json.dumps(self._shared), encoding="utf-8")
        if self._mine is not None:
            sl._CONN_STATE_DIR.mkdir(parents=True, exist_ok=True)
            (sl._CONN_STATE_DIR / f"{_MINE}.json").write_text(
                json.dumps(self._mine), encoding="utf-8"
            )
        if self._breaker is not None:
            sl._BREAKER_PATH.write_text(json.dumps(self._breaker), encoding="utf-8")
        return self

    def __exit__(self, *_exc):
        (
            sl._SERVER_READY_PATH,
            sl._CONN_STATE_DIR,
            sl._BREAKER_PATH,
            sl._CONFIG_PATH,
        ) = self._orig
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self._dir, ignore_errors=True)
        return False


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


# ── our own record is what this terminal shows ─────────────────────────────


def test_own_ready_record_renders_the_dot():
    with _Markers(mine=_rec("ready", _MINE)):
        assert sl._health_prefix(_MINE) == _READY


def test_own_failure_record_renders_its_reason():
    with _Markers(mine=_rec("auth_failed", _MINE)):
        assert sl._health_prefix(_MINE) == f"✕ ({sl._COGNEE_KEY_REASON}) "


def test_two_terminals_can_disagree():
    """The point of the whole exercise: their auth_failed is not our verdict."""
    with _Markers(shared=_rec("auth_failed", _THEIRS), mine=_rec("ready", _MINE)):
        assert sl._health_prefix(_MINE) == _READY


# ── falling back to the shared marker ─────────────────────────────────────


def test_unattributed_shared_marker_is_used():
    """Pre-upgrade writer, or a write made before the session key was known."""
    with _Markers(shared=_rec("ready")):
        assert sl._health_prefix(_MINE) == _READY


def test_legacy_marker_without_state_reads_as_ready():
    with _Markers(shared={"ready_at": time.time(), "base_url": _URL}):
        assert sl._health_prefix(_MINE) == _READY


def test_another_sessions_marker_is_ignored_when_we_have_none():
    with _Markers(shared=_rec("auth_failed", _THEIRS)):
        assert sl._health_prefix(_MINE) == ""


def test_another_sessions_ready_does_not_green_us():
    with _Markers(shared=_rec("ready", _THEIRS)):
        assert sl._health_prefix(_MINE) == ""


def test_caller_without_a_session_id_uses_the_shared_marker():
    with _Markers(shared=_rec("ready", _THEIRS)):
        assert sl._health_prefix("") == _READY


# ── a fresher shared FAILURE wins: the server is shared ───────────────────


def test_fresher_shared_failure_overrides_our_older_ready():
    with _Markers(
        shared=_rec("unreachable", _THEIRS, age=1),
        mine=_rec("ready", _MINE, age=600),
    ):
        assert sl._health_prefix(_MINE) == "✕ (unreachable) "


def test_our_fresher_ready_beats_a_stale_shared_failure():
    with _Markers(
        shared=_rec("unreachable", _THEIRS, age=600),
        mine=_rec("ready", _MINE, age=1),
    ):
        assert sl._health_prefix(_MINE) == _READY


def test_a_fresher_shared_ready_does_not_clear_our_failure():
    """Only failures cross session boundaries — their working key is not ours."""
    with _Markers(
        shared=_rec("ready", _THEIRS, age=1),
        mine=_rec("auth_failed", _MINE, age=600),
    ):
        assert sl._health_prefix(_MINE) == f"✕ ({sl._COGNEE_KEY_REASON}) "


# ── the pre-existing rules still hold ────────────────────────────────────


def test_open_breaker_overrides_our_ready():
    with _Markers(
        mine=_rec("ready", _MINE),
        breaker={"cooldown_until": time.time() + 60},
    ):
        assert sl._health_prefix(_MINE) == "✕ (unreachable) "


def test_expired_breaker_leaves_ready_alone():
    with _Markers(
        mine=_rec("ready", _MINE),
        breaker={"cooldown_until": time.time() - 60},
    ):
        assert sl._health_prefix(_MINE) == _READY


def test_base_url_mismatch_is_ignored():
    """A local-ready record must never green a cloud session."""
    with _Markers(mine=_rec("ready", _MINE, base_url="https://other.example.com")):
        assert sl._health_prefix(_MINE) == ""


def test_no_markers_at_all_renders_no_glyph():
    with _Markers():
        assert sl._health_prefix(_MINE) == ""


def test_path_unsafe_session_id_is_not_used_as_a_path():
    with _Markers(shared=_rec("ready")):
        assert sl._health_prefix("../../etc/passwd") == _READY


# ── A: only server-wide failures cross session boundaries ─────────────────


def test_another_sessions_auth_failure_does_not_leak_into_our_bar():
    """The regression: a keyless CLOUD terminal starting up must not red a healthy
    LOCAL one. `auth_failed` describes that session's credential, not the server."""
    with _Markers(
        shared=_rec("auth_failed", _THEIRS, age=1, base_url="https://tenant-x.aws.cognee.ai"),
        mine=_rec("ready", _MINE, age=30, base_url=sl._DEFAULT_LOCAL_BASE_URL),
        base_url=None,  # nothing exported — the default local setup
    ):
        assert sl._health_prefix(_MINE) == _READY


def test_another_sessions_auth_failure_on_our_own_target_does_not_leak_either():
    """Even on the same URL: their key being rejected says nothing about ours."""
    with _Markers(
        shared=_rec("auth_failed", _THEIRS, age=1),
        mine=_rec("ready", _MINE, age=30),
    ):
        assert sl._health_prefix(_MINE) == _READY


def test_fresher_shared_server_error_still_overrides():
    with _Markers(
        shared=_rec("server_error", _THEIRS, age=1),
        mine=_rec("ready", _MINE, age=600),
    ):
        assert sl._health_prefix(_MINE) == "✕ (server_error) "


# ── C: the mismatch guard has a URL to compare against ────────────────────


def test_active_base_url_defaults_to_localhost():
    """Without this the guard is toothless: no URL of our own means no mismatch."""
    with _Markers(base_url=None):
        assert sl._active_base_url() == sl._DEFAULT_LOCAL_BASE_URL


def test_active_base_url_honours_the_local_api_url_var_first():
    """Mirrors _plugin_common._local_api_url_with_source, which stamps the markers."""
    with _Markers(base_url=_URL):
        os.environ["COGNEE_LOCAL_API_URL"] = "http://127.0.0.1:9999/"
        assert sl._active_base_url() == "http://127.0.0.1:9999"


def test_a_cloud_marker_is_rejected_by_a_default_local_session():
    """No record of our own, and the shared one is for a different server."""
    with _Markers(
        shared=_rec("unreachable", None, age=1, base_url="https://tenant-x.aws.cognee.ai"),
        base_url=None,
    ):
        assert sl._health_prefix(_MINE) == ""


def test_our_own_localhost_record_still_matches_the_default():
    with _Markers(mine=_rec("ready", _MINE, base_url=sl._DEFAULT_LOCAL_BASE_URL), base_url=None):
        assert sl._health_prefix(_MINE) == _READY


# ── containment: an accepted session id can never leave its directory ───────


def test_path_safe_rejects_every_separator():
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


def test_accepted_ids_always_stay_inside_the_directory():
    """Exhaustive over an adversarial alphabet: no accepted id escapes."""
    import itertools

    root = pathlib.Path("/tmp/cognee-path-guard")
    for n in (1, 2, 3):
        for tup in itertools.product("a1._-/\\:~\0 ", repeat=n):
            sid = "".join(tup)
            if not sl._path_safe(sid):
                continue
            assert (root / f"{sid}.json").resolve().parent == root.resolve(), sid


def test_dotdot_becomes_a_filename_not_a_parent_hop():
    root = pathlib.Path("/tmp/cognee-path-guard")
    assert sl._path_safe("..")
    assert (root / "...json").resolve() == (root / f"{'..'}.json").resolve()
    assert (root / f"{'..'}.json").resolve().parent == root.resolve()


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
