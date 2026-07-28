"""Tests for the LLM-key signal in the status line (cognee_statusline_render.py).

`LLM_API_KEY` problems share the ONE leading glyph slot with the server-connection
signal (they used to be a trailing segment on the far right of the bar, which read
as unrelated noise). The slot holds a single sign, by precedence:

  1. a server-connection failure wins -- if the server can't be reached or
     authenticated, its LLM key is not the actionable problem;
  2. otherwise an LLM-key failure, shown *in place of* the green ● (a ● next to
     an ✕ reads as contradictory);
  3. otherwise the server signal itself (``● `` or nothing).

No unittest.mock, matching this test directory's convention: the module-level
marker-path constants are reassigned to tmp paths and restored in `finally`.
COGNEE_BASE_URL is set explicitly so `_active_mode()` short-circuits before it
would read the developer's real ~/.cognee-plugin config.

Run: python integrations/claude-code/tests/test_statusline_llm_state.py
(or via pytest).
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import cognee_statusline_render as sl  # noqa: E402

_LOCAL_URL = "http://127.0.0.1:8000"
_CLOUD_URL = "https://api.example-cognee.ai"
# Both marker states — no key at all, and a key the provider rejected — render as one
# red label, because the user's fix is the same either way. The names are kept apart so
# each test still says which state it wrote.
_NO_KEY = sl._fail_glyph(sl._LLM_KEY_REASON)
_AUTH_FAILED = _NO_KEY
_LOCAL = f"{sl._MODE_STYLES['local']}local\033[0m"  # the mode is styled in the bar


class _Renderer:
    """Point the renderer's markers at a tmp dir and pin the active base_url.

    Every marker path the glyph slot reads is redirected -- including the breaker
    and server-ready markers, so a real one in the developer's home can't leak in.
    """

    def __init__(self, llm_state=None, server_marker=None, base_url=_LOCAL_URL, own_state=None):
        self._llm_state = llm_state
        self._server_marker = server_marker
        self._base_url = base_url
        # own_state lands in llm-state/my-session.json — what THIS terminal observed.
        self._own_state = own_state

    def __enter__(self):
        self._dir = pathlib.Path(tempfile.mkdtemp())
        self._orig = (
            sl._LLM_STATE_PATH,
            sl._SERVER_READY_PATH,
            sl._BREAKER_PATH,
            sl._LLM_STATE_DIR,
        )
        self._orig_url = os.environ.get("COGNEE_BASE_URL")

        sl._LLM_STATE_PATH = self._dir / "llm-state.json"
        sl._SERVER_READY_PATH = self._dir / "server-ready.json"
        sl._BREAKER_PATH = self._dir / "recall-breaker.json"  # never written
        sl._LLM_STATE_DIR = self._dir / "llm-state"
        os.environ["COGNEE_BASE_URL"] = self._base_url

        if self._llm_state is not None:
            _write(sl._LLM_STATE_PATH, self._llm_state)
        if self._server_marker is not None:
            _write(sl._SERVER_READY_PATH, self._server_marker)
        if self._own_state is not None:
            _write(sl._LLM_STATE_DIR / "my-session.json", self._own_state)
        return self

    def __exit__(self, *_exc):
        (
            sl._LLM_STATE_PATH,
            sl._SERVER_READY_PATH,
            sl._BREAKER_PATH,
            sl._LLM_STATE_DIR,
        ) = self._orig
        if self._orig_url is None:
            os.environ.pop("COGNEE_BASE_URL", None)
        else:
            os.environ["COGNEE_BASE_URL"] = self._orig_url
        shutil.rmtree(self._dir, ignore_errors=True)
        return False


def _write(path: pathlib.Path, payload):
    """Write a marker, stamping a fresh `checked_at` unless the test set one.

    Verdicts have a TTL (`_LLM_STATE_STALE_SECONDS`), so an unstamped marker would
    read as stale and every glyph assertion would trivially pass on "".
    """
    if isinstance(payload, dict) and "llm_state" in payload:
        payload = {"checked_at": time.time(), **payload}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


# ── no file / malformed / healthy → nothing shown ───────────────────────────


def test_no_marker_file_returns_empty():
    with _Renderer():  # llm-state.json never written
        assert sl._llm_prefix() == ""


def test_malformed_marker_returns_empty():
    with _Renderer(llm_state="not json{{{"):
        assert sl._llm_prefix() == ""


def test_marker_without_state_returns_empty():
    with _Renderer(llm_state={"checked_at": time.time()}):
        assert sl._llm_prefix() == ""


def test_state_ok_returns_empty():
    with _Renderer(llm_state={"llm_state": "ok"}):
        assert sl._llm_prefix() == ""


# ── verdicts expire: the marker is shared by every session ──────────────────


def test_stale_verdict_is_ignored():
    """A verdict from a session that is long gone must stop accusing the key."""
    stale = time.time() - sl._LLM_STATE_STALE_SECONDS - 60
    with _Renderer(llm_state={"llm_state": "not_set", "checked_at": stale}):
        assert sl._llm_prefix() == ""


def test_verdict_just_inside_the_window_still_shows():
    fresh = time.time() - sl._LLM_STATE_STALE_SECONDS + 60
    with _Renderer(llm_state={"llm_state": "not_set", "checked_at": fresh}):
        assert sl._llm_prefix() == _NO_KEY


def test_verdict_without_checked_at_is_ignored():
    with _Renderer(llm_state={"llm_state": "auth_failed", "checked_at": None}):
        assert sl._llm_prefix() == ""


def test_unparseable_checked_at_is_ignored():
    with _Renderer(llm_state={"llm_state": "auth_failed", "checked_at": "yesterday"}):
        assert sl._llm_prefix() == ""


# ── attribution: a keyless session must not accuse everyone else ────────────


def test_other_sessions_verdict_is_hidden():
    """Observed for real: a launch without the export wrote not_set machine-wide."""
    with _Renderer(llm_state={"llm_state": "not_set", "session_key": "other-session"}):
        assert sl._llm_prefix("my-session") == ""
        assert sl._status_prefix("my-session") == ""


def test_own_verdict_is_shown():
    with _Renderer(llm_state={"llm_state": "not_set", "session_key": "my-session"}):
        assert sl._llm_prefix("my-session") == _NO_KEY


def test_unattributed_verdict_is_still_shown():
    """Older marker with no session_key — keep the signal rather than lose it."""
    with _Renderer(llm_state={"llm_state": "auth_failed"}):
        assert sl._llm_prefix("my-session") == _AUTH_FAILED


def test_caller_without_a_session_id_sees_any_verdict():
    with _Renderer(llm_state={"llm_state": "not_set", "session_key": "other-session"}):
        assert sl._llm_prefix("") == _NO_KEY


# ── failures → a LEFT glyph, trailing space, no leading padding ─────────────


def test_not_set_returns_left_glyph():
    with _Renderer(llm_state={"llm_state": "not_set"}):
        prefix = sl._llm_prefix()
        assert prefix == _NO_KEY
        assert not prefix.startswith(" "), "must not carry the old trailing-segment padding"
        assert prefix.endswith(" "), "concatenates directly onto 'cognee: '"


def test_auth_failed_returns_left_glyph():
    with _Renderer(llm_state={"llm_state": "auth_failed"}):
        assert sl._llm_prefix() == _AUTH_FAILED


def test_color_resets_before_the_separating_space():
    """A dangling ANSI sequence would tint the rest of the bar."""
    with _Renderer(llm_state={"llm_state": "not_set"}):
        assert sl._llm_prefix().endswith("\033[0m ")


# ── cloud mode: the local server's LLM key is not ours to report ────────────


def test_cloud_mode_suppresses_llm_glyph():
    with _Renderer(llm_state={"llm_state": "auth_failed"}, base_url=_CLOUD_URL):
        assert sl._llm_prefix() == ""
        assert sl._status_prefix() == ""


# ── precedence inside the single slot ───────────────────────────────────────


def test_server_failure_wins_over_llm_failure():
    with _Renderer(
        llm_state={"llm_state": "not_set"},
        server_marker={"state": "auth_failed", "base_url": _LOCAL_URL},
    ):
        assert sl._status_prefix() == sl._fail_glyph(sl._COGNEE_KEY_REASON)


def test_llm_failure_replaces_the_ready_dot():
    with _Renderer(
        llm_state={"llm_state": "not_set"},
        server_marker={"state": "ready", "base_url": _LOCAL_URL},
    ):
        prefix = sl._status_prefix()
        assert prefix == _NO_KEY
        assert "●" not in prefix, "● and ✕ side by side read as contradictory"


def test_ready_dot_survives_when_the_llm_key_is_fine():
    with _Renderer(
        llm_state={"llm_state": "ok"},
        server_marker={"state": "ready", "base_url": _LOCAL_URL},
    ):
        assert sl._status_prefix() == sl._ok_glyph()


def test_llm_failure_shows_when_the_server_state_is_unknown():
    with _Renderer(llm_state={"llm_state": "auth_failed"}):  # no server marker
        assert sl._status_prefix() == _AUTH_FAILED


# ── end-to-end: the sign renders to the LEFT of "cognee:" ──────────────────


def test_full_render_places_the_glyph_before_the_label():
    """Runs the renderer as Claude Code does, with a fake plugin-enabled HOME."""
    with tempfile.TemporaryDirectory() as home:
        home_path = pathlib.Path(home)
        plugin_dir = home_path / ".cognee-plugin" / "claude-code"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "llm-state.json").write_text(
            json.dumps({"llm_state": "not_set", "checked_at": time.time()}), encoding="utf-8"
        )
        # Server is up and authenticated: only the LLM key is broken.
        (home_path / ".cognee-plugin" / "server-ready.json").write_text(
            json.dumps({"state": "ready", "base_url": _LOCAL_URL}), encoding="utf-8"
        )
        claude_dir = home_path / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text(
            '{"enabledPlugins": {"cognee-memory@cognee": true}}', encoding="utf-8"
        )

        env = os.environ.copy()
        env["HOME"] = home  # POSIX
        env["USERPROFILE"] = home  # Windows: Path.home() prefers this
        env["PYTHONIOENCODING"] = "utf-8"
        env["COGNEE_BASE_URL"] = _LOCAL_URL
        env["COGNEE_UPDATE_CHECK"] = "0"  # suppress the update segment
        env.pop("COGNEE_PLUGIN_DATASET", None)  # -> default dataset

        proc = subprocess.run(
            [sys.executable, str(_SCRIPTS / "cognee_statusline_render.py")],
            input=b"{}",
            capture_output=True,
            env=env,
        )
        out = proc.stdout.decode("utf-8")
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        assert out == f"{_NO_KEY}cognee: agent_sessions · {_LOCAL}", repr(out)


# ── per-session verdicts: one terminal has the key, the other doesn't ───────


def test_own_verdict_file_is_preferred_over_the_shared_one():
    """The exact scenario: their "ok" must not green a bar with no key."""
    with _Renderer(
        llm_state={"llm_state": "ok", "session_key": "their-session"},
        own_state={"llm_state": "not_set"},
    ):
        assert sl._llm_prefix("my-session") == _NO_KEY


def test_own_ok_is_not_reddened_by_another_sessions_not_set():
    with _Renderer(
        llm_state={"llm_state": "not_set", "session_key": "their-session"},
        own_state={"llm_state": "ok"},
    ):
        assert sl._llm_prefix("my-session") == ""


def test_own_stale_verdict_still_expires():
    stale = time.time() - sl._LLM_STATE_STALE_SECONDS - 60
    with _Renderer(own_state={"llm_state": "not_set", "checked_at": stale}):
        assert sl._llm_prefix("my-session") == ""


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
