"""Tests for the LLM-key signal in the status line (Codex renderer).

`LLM_API_KEY` problems share the ONE leading glyph slot with the server-connection
signal (they used to be a trailing segment at the far right, which read as
unrelated noise). The slot holds a single sign, by precedence:

  1. a server-connection failure wins -- if the server can't be reached or
     authenticated, its LLM key is not the actionable problem;
  2. otherwise an LLM-key failure, shown *in place of* the ● (a ● next to an ✕
     reads as contradictory);
  3. otherwise the server signal itself (``● `` or nothing).

Plain text, no ANSI: Codex injects this status into the model's context rather
than a terminal bar. Both emitters (`render_status_for_host`, used by the hooks,
and `main`, used by cognee-statusline.sh) are covered.

No unittest.mock, matching the convention in these test dirs: module-level
marker-path constants are reassigned to tmp paths and restored in `finally`.
COGNEE_BASE_URL is set explicitly so `_active_mode()` short-circuits before it
would read the developer's real ~/.cognee-plugin config.

Run: python integrations/codex/tests/test_statusline_llm_state.py (or via pytest).
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import cognee_statusline_render as sl  # noqa: E402

_LOCAL_URL = "http://127.0.0.1:8000"
_CLOUD_URL = "https://api.example-cognee.ai"
_NO_KEY = "✕ (llm_no_key) "
_AUTH_FAILED = "✕ (llm_auth_failed) "


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


# ── failures → a LEADING glyph, trailing space, no leading padding ──────────


def test_not_set_returns_leading_glyph():
    with _Renderer(llm_state={"llm_state": "not_set"}):
        prefix = sl._llm_prefix()
        assert prefix == _NO_KEY
        assert not prefix.startswith(" "), "must not carry the old trailing-segment padding"
        assert prefix.endswith(" "), "concatenates directly onto 'cognee: '"


def test_auth_failed_returns_leading_glyph():
    with _Renderer(llm_state={"llm_state": "auth_failed"}):
        assert sl._llm_prefix() == _AUTH_FAILED


def test_no_ansi_escapes():
    """Codex renders into model context, not a terminal — keep it plain."""
    with _Renderer(llm_state={"llm_state": "not_set"}):
        assert "\033" not in sl._llm_prefix()


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
        assert sl._status_prefix() == "✕ (auth_failed) "


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
        assert sl._status_prefix() == "● "


def test_llm_failure_shows_when_the_server_state_is_unknown():
    with _Renderer(llm_state={"llm_state": "auth_failed"}):  # no server marker
        assert sl._status_prefix() == _AUTH_FAILED


# ── the hook-facing emitter puts it left of the label ───────────────────────


def test_render_status_for_host_places_the_glyph_before_the_label():
    orig_update = os.environ.get("COGNEE_UPDATE_CHECK")
    os.environ["COGNEE_UPDATE_CHECK"] = "0"  # suppress the update segment
    orig_dataset = os.environ.pop("COGNEE_PLUGIN_DATASET", None)
    try:
        with _Renderer(
            llm_state={"llm_state": "not_set"},
            server_marker={"state": "ready", "base_url": _LOCAL_URL},
        ):
            out = sl.render_status_for_host("session-key")
            assert out == f"{_NO_KEY}cognee: agent_sessions · local", repr(out)
    finally:
        if orig_update is None:
            os.environ.pop("COGNEE_UPDATE_CHECK", None)
        else:
            os.environ["COGNEE_UPDATE_CHECK"] = orig_update
        if orig_dataset is not None:
            os.environ["COGNEE_PLUGIN_DATASET"] = orig_dataset


# ── end-to-end via cognee-statusline.sh's entrypoint ───────────────────────


def test_full_render_places_the_glyph_before_the_label():
    with tempfile.TemporaryDirectory() as home:
        home_path = pathlib.Path(home)
        (home_path / ".cognee-plugin" / "codex").mkdir(parents=True)
        (home_path / ".cognee-plugin" / "codex" / "llm-state.json").write_text(
            json.dumps({"llm_state": "not_set", "checked_at": time.time()}), encoding="utf-8"
        )
        # Server is up and authenticated: only the LLM key is broken.
        (home_path / ".cognee-plugin" / "server-ready.json").write_text(
            json.dumps({"state": "ready", "base_url": _LOCAL_URL}), encoding="utf-8"
        )

        env = os.environ.copy()
        env["HOME"] = home  # POSIX
        env["USERPROFILE"] = home  # Windows: Path.home() prefers this
        env["PYTHONIOENCODING"] = "utf-8"
        env["COGNEE_BASE_URL"] = _LOCAL_URL
        env["COGNEE_UPDATE_CHECK"] = "0"
        env.pop("COGNEE_PLUGIN_DATASET", None)

        proc = subprocess.run(
            [sys.executable, str(_SCRIPTS / "cognee_statusline_render.py")],
            input=b"{}",
            capture_output=True,
            env=env,
        )
        out = proc.stdout.decode("utf-8")
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        assert out == f"{_NO_KEY}cognee: agent_sessions · local", repr(out)


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
