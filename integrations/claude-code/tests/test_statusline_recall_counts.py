"""Tests for `_recall_segment` (cognee_statusline_render.py) — the "what memory did
last turn" counts at the end of the Claude Code status line.

`session-context-lookup` already wrote `last_recall.json` for exactly this purpose;
these lock down the rendering contract:

  * shape: ` · recall <s>s/<t>t/<g>g/<a>a · saved <p>p/<t>t/<a>a`, faint, reset at the
    end so no color bleeds into the rest of the bar;
  * attribution: the marker is per-integration, not per-session, so another live
    session's counts must not appear in this bar — but an unattributable marker
    (no `session_key`) or a host context without `session_id` still renders, since a
    slightly lagging number beats a segment that silently never appears;
  * a missing/malformed/`hits`-less marker renders nothing and never raises.

No unittest.mock, matching this test directory's convention: the module-level
`_RECALL_PATH` is reassigned to a tmp path and restored in `finally`.

Run: python integrations/claude-code/tests/test_statusline_recall_counts.py
(or via pytest).
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import cognee_statusline_render as sl  # noqa: E402

_SESSION = "fde122ae-07db-431d-b5af-acba353e4e3e"
_HITS = {"session": 4, "trace": 5, "graph_context": 0, "session_context": 1}
_SAVES = {"prompt": 2, "trace": 41, "answer": 2}
_FULL = " \033[2m· recall 4s/5t/0g/1a · saved 2p/41t/2a\033[0m"
_LOCAL = f"{sl._MODE_STYLES['local']}local\033[0m"  # the mode is styled in the bar


class _Marker:
    """Point the renderer's recall paths at a tmp dir.

    `payload` is the machine-wide `last_recall.json` (str written verbatim);
    `per_session` maps a session key to its own `recall/<key>.json` copy.
    """

    def __init__(self, payload=None, per_session=None):
        self._payload = payload
        self._per_session = per_session or {}

    def __enter__(self):
        self._dir = pathlib.Path(tempfile.mkdtemp())
        self._orig = (sl._RECALL_PATH, sl._RECALL_DIR)
        self._orig_off = os.environ.pop("COGNEE_STATUSLINE_COUNTS", None)
        sl._RECALL_PATH = self._dir / "last_recall.json"
        sl._RECALL_DIR = self._dir / "recall"
        if self._payload is not None:
            sl._RECALL_PATH.write_text(
                self._payload if isinstance(self._payload, str) else json.dumps(self._payload),
                encoding="utf-8",
            )
        for key, payload in self._per_session.items():
            sl._RECALL_DIR.mkdir(parents=True, exist_ok=True)
            (sl._RECALL_DIR / f"{key}.json").write_text(
                payload if isinstance(payload, str) else json.dumps(payload),
                encoding="utf-8",
            )
        return self

    def __exit__(self, *_exc):
        sl._RECALL_PATH, sl._RECALL_DIR = self._orig
        if self._orig_off is not None:
            os.environ["COGNEE_STATUSLINE_COUNTS"] = self._orig_off
        shutil.rmtree(self._dir, ignore_errors=True)
        return False


# ── nothing to show ─────────────────────────────────────────────────────────


def test_no_marker_file_renders_nothing():
    with _Marker():
        assert sl._recall_segment(_SESSION) == ""


def test_malformed_marker_renders_nothing():
    with _Marker("not json{{{"):
        assert sl._recall_segment(_SESSION) == ""


def test_marker_without_hits_renders_nothing():
    with _Marker({"session_key": _SESSION, "ts": "2026-07-28T10:04:17+00:00"}):
        assert sl._recall_segment(_SESSION) == ""


def test_non_dict_hits_renders_nothing():
    with _Marker({"session_key": _SESSION, "hits": "lots"}):
        assert sl._recall_segment(_SESSION) == ""


# ── the rendering contract ─────────────────────────────────────────────────


def test_counts_render_with_recall_and_saves():
    with _Marker({"session_key": _SESSION, "hits": _HITS, "saves_last_turn": _SAVES}):
        assert sl._recall_segment(_SESSION) == _FULL


def test_zero_counts_still_render():
    """0g is information — graph was searched and returned nothing."""
    zeros = {"session": 0, "trace": 0, "graph_context": 0, "session_context": 0}
    with _Marker({"session_key": _SESSION, "hits": zeros}):
        assert sl._recall_segment(_SESSION) == " \033[2m· recall 0s/0t/0g/0a\033[0m"


def test_saves_omitted_when_absent():
    with _Marker({"session_key": _SESSION, "hits": _HITS}):
        assert sl._recall_segment(_SESSION) == " \033[2m· recall 4s/5t/0g/1a\033[0m"


def test_missing_and_unparseable_counts_read_as_zero():
    with _Marker({"session_key": _SESSION, "hits": {"session": "x", "trace": 5}}):
        assert sl._recall_segment(_SESSION) == " \033[2m· recall 0s/5t/0g/0a\033[0m"


def test_color_resets_at_the_end():
    with _Marker({"session_key": _SESSION, "hits": _HITS}):
        assert sl._recall_segment(_SESSION).endswith("\033[0m")


# ── attribution: never show another session's counts ──────────────────────


def test_other_sessions_counts_are_hidden():
    with _Marker({"session_key": "some-other-session", "hits": _HITS}):
        assert sl._recall_segment(_SESSION) == ""


def test_unattributed_marker_still_renders():
    """Older marker with no session_key — lagging counts beat no counts."""
    with _Marker({"hits": _HITS}):
        assert sl._recall_segment(_SESSION) == " \033[2m· recall 4s/5t/0g/1a\033[0m"


def test_host_context_without_session_id_still_renders():
    with _Marker({"session_key": "some-other-session", "hits": _HITS}):
        assert sl._recall_segment("") == " \033[2m· recall 4s/5t/0g/1a\033[0m"


# ── per-session copy: every terminal shows its OWN numbers ────────────────


def test_per_session_file_is_preferred():
    """Concurrent terminals: the shared file holds whoever prompted last."""
    mine = {"session": 9, "trace": 8, "graph_context": 7, "session_context": 6}
    with _Marker(
        payload={"session_key": "noisy-neighbour", "hits": _HITS, "saves_last_turn": _SAVES},
        per_session={_SESSION: {"hits": mine}},
    ):
        assert sl._recall_segment(_SESSION) == " \033[2m· recall 9s/8t/7g/6a\033[0m"


def test_falls_back_to_the_shared_file_when_no_per_session_copy():
    with _Marker(payload={"session_key": _SESSION, "hits": _HITS}):
        assert sl._recall_segment(_SESSION) == " \033[2m· recall 4s/5t/0g/1a\033[0m"


def test_another_sessions_per_session_copy_is_never_read():
    with _Marker(per_session={"someone-else": {"hits": _HITS}}):
        assert sl._recall_segment(_SESSION) == ""


def test_path_unsafe_session_id_does_not_escape_the_recall_dir():
    """The id arrives from stdin JSON; never build a path from it unchecked."""
    with _Marker(payload={"hits": _HITS}):
        assert sl._recall_segment("../../etc/passwd") == " \033[2m· recall 4s/5t/0g/1a\033[0m"


# ── opt-out ───────────────────────────────────────────────────────────────


def test_env_opt_out_suppresses_the_segment():
    with _Marker({"session_key": _SESSION, "hits": _HITS}):
        os.environ["COGNEE_STATUSLINE_COUNTS"] = "false"
        try:
            assert sl._recall_segment(_SESSION) == ""
        finally:
            os.environ.pop("COGNEE_STATUSLINE_COUNTS", None)


# ── end-to-end: the counts land at the END of the bar ─────────────────────


def test_full_render_places_counts_after_the_mode():
    with tempfile.TemporaryDirectory() as home:
        home_path = pathlib.Path(home)
        plugin_dir = home_path / ".cognee-plugin" / "claude-code"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "last_recall.json").write_text(
            json.dumps({"session_key": _SESSION, "hits": _HITS, "saves_last_turn": _SAVES}),
            encoding="utf-8",
        )
        (home_path / ".cognee-plugin" / "server-ready.json").write_text(
            json.dumps({"state": "ready", "base_url": "http://127.0.0.1:8000"}),
            encoding="utf-8",
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
        env["COGNEE_BASE_URL"] = "http://127.0.0.1:8000"
        env["COGNEE_UPDATE_CHECK"] = "0"
        env.pop("COGNEE_PLUGIN_DATASET", None)
        env.pop("COGNEE_STATUSLINE_COUNTS", None)

        proc = subprocess.run(
            [sys.executable, str(_SCRIPTS / "cognee_statusline_render.py")],
            input=json.dumps({"session_id": _SESSION}).encode("utf-8"),
            capture_output=True,
            env=env,
        )
        out = proc.stdout.decode("utf-8")
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        assert out == f"{sl._ok_glyph()}cognee: agent_sessions · {_LOCAL}{_FULL}", repr(out)


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
