"""Tests for `spawn_server_healer` / `is_local_url` / `_claim_healer_cooldown`
(_plugin_common.py) -- the fire-and-forget re-bootstrap of a locally-down
Cognee server, and the gate that decides when it's appropriate to try.

Why this exists: COGNEE_AGENT_MODE's watchdog (in the cognee package itself)
tears the local server down 60s after the last registered agent connection
unregisters -- well before a genuine SessionEnd, across a long, multi-resume
conversation. session-start.py's bootstrap only runs at the real SessionStart
hook point, so nothing previously re-triggered it mid-session; recall then
gracefully skipped (by design) turn after turn with no automatic recovery.

A real bug was caught by review before this shipped: the original call site
gated on `runtime["mode"] != "http"`, but `resolve_runtime_mode()`'s mode is
"http" for BOTH a local server and a real remote cloud endpoint (the plugin's
own default local setup falls back to http://localhost:8011, so there is no
configuration under which mode is ever anything else) -- the healer would
never have fired in any real deployment. `test_is_local_url_distinguishes_
local_from_cloud` below is the regression test for that specific class of
mistake: it must ALWAYS be checked against the actual URL host, never a mode
label that conflates local-via-HTTP with cloud-via-HTTP.

No unittest.mock here, matching this test directory's existing convention:
module-level path constants are reassigned directly to tmp paths (and restored
in `finally`, alongside their pre-test env-var values, not just popped to
absent), and the real subprocess call is exercised against a tiny, harmless
throwaway script rather than the real session-start.py, so this suite never
touches the real shared ~/.cognee-plugin/ state or attempts a real Cognee
bootstrap.

Run: python integrations/claude-code/tests/test_server_healer.py
(or via pytest).
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _plugin_common as pc  # noqa: E402


def _tmp_dir():
    return pathlib.Path(tempfile.mkdtemp())


class _EnvVar:
    """Save/restore a single env var's ORIGINAL value (not just pop-to-absent),
    so this suite never clobbers a real ambient value if run in a shell/session
    where one happens to be exported already."""

    def __init__(self, name, value):
        self.name = name
        self.value = value
        self._had = name in os.environ
        self._orig = os.environ.get(name)

    def __enter__(self):
        if self.value is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.value
        return self

    def __exit__(self, *exc):
        if self._had:
            os.environ[self.name] = self._orig
        else:
            os.environ.pop(self.name, None)


# ── is_local_url: the exact gate a real bug shipped with (never checked) ────

def test_is_local_url_distinguishes_local_from_cloud():
    assert pc.is_local_url("http://localhost:8011") is True
    assert pc.is_local_url("http://127.0.0.1:8011") is True
    assert pc.is_local_url("http://0.0.0.0:8011") is True
    assert pc.is_local_url("https://my-org.cognee.ai") is False
    assert pc.is_local_url("https://api.cognee.cloud") is False


def test_resolve_runtime_mode_is_http_for_both_local_and_cloud():
    # Documents the actual root cause: mode alone cannot gate this decision.
    with _EnvVar("COGNEE_BASE_URL", None):
        local_runtime = pc.resolve_runtime_mode()
    assert local_runtime["mode"] == "http"
    assert pc.is_local_url(local_runtime["base_url"]) is True

    with _EnvVar("COGNEE_BASE_URL", "https://my-org.cognee.ai"):
        cloud_runtime = pc.resolve_runtime_mode()
    assert cloud_runtime["mode"] == "http"  # same mode string as the local case
    assert pc.is_local_url(cloud_runtime["base_url"]) is False  # but distinguishable


# ── spawn_server_healer / _claim_healer_cooldown ────────────────────────────

def test_returns_false_when_session_start_script_missing():
    orig = pc._SESSION_START_SCRIPT
    tmp = _tmp_dir()
    try:
        pc._SESSION_START_SCRIPT = tmp / "does-not-exist.py"
        assert pc.spawn_server_healer(cwd=str(tmp)) is False
    finally:
        pc._SESSION_START_SCRIPT = orig
        shutil.rmtree(tmp, ignore_errors=True)


def test_spawns_via_bootstrap_cli_arg_not_stdin():
    # A tiny, harmless throwaway script standing in for session-start.py --
    # proves the actual invocation shape (argv, not stdin/a temp file) works
    # end-to-end without ever attempting a real Cognee bootstrap.
    tmp = _tmp_dir()
    dummy = tmp / "dummy_session_start.py"
    echo_path = tmp / "echoed.json"
    dummy.write_text(
        "import sys, json\n"
        "assert sys.argv[1] == '--bootstrap'\n"
        f"open(r'{echo_path}', 'w', encoding='utf-8').write(sys.argv[2])\n",
        encoding="utf-8",
    )
    orig_script = pc._SESSION_START_SCRIPT
    orig_marker = pc._HEALER_SPAWN_MARKER
    orig_log = pc._HEALER_LOG
    try:
        pc._SESSION_START_SCRIPT = dummy
        pc._HEALER_SPAWN_MARKER = tmp / "healer-spawned.json"
        pc._HEALER_LOG = tmp / "healer.log"
        with _EnvVar("COGNEE_SESSION_KEY", "test-session-key"):
            result = pc.spawn_server_healer(cwd=str(tmp))
        assert result is True
        assert pc._HEALER_SPAWN_MARKER.exists()

        deadline = time.monotonic() + 5.0
        while not echo_path.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert echo_path.exists(), "dummy script never received the --bootstrap argv"
        payload = json.loads(echo_path.read_text(encoding="utf-8"))
        assert payload["session_id"] == "test-session-key"
        assert payload["cwd"] == str(tmp)
    finally:
        pc._SESSION_START_SCRIPT = orig_script
        pc._HEALER_SPAWN_MARKER = orig_marker
        pc._HEALER_LOG = orig_log
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_temp_file_is_created_by_a_spawn():
    # Regression: the original implementation wrote the payload to a real temp
    # file that was never cleaned up. Confirm the fixed version creates none.
    tmp = _tmp_dir()
    dummy = tmp / "dummy_session_start.py"
    dummy.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    orig_script = pc._SESSION_START_SCRIPT
    orig_marker = pc._HEALER_SPAWN_MARKER
    orig_log = pc._HEALER_LOG
    before = set(pathlib.Path(tempfile.gettempdir()).glob("*.json"))
    try:
        pc._SESSION_START_SCRIPT = dummy
        pc._HEALER_SPAWN_MARKER = tmp / "healer-spawned.json"
        pc._HEALER_LOG = tmp / "healer.log"
        assert pc.spawn_server_healer(cwd=str(tmp)) is True
        time.sleep(0.2)
        after = set(pathlib.Path(tempfile.gettempdir()).glob("*.json"))
        assert after - before == set(), "spawn_server_healer left a stray temp file behind"
    finally:
        pc._SESSION_START_SCRIPT = orig_script
        pc._HEALER_SPAWN_MARKER = orig_marker
        pc._HEALER_LOG = orig_log
        shutil.rmtree(tmp, ignore_errors=True)


def test_cooldown_prevents_a_second_spawn_within_the_window():
    tmp = _tmp_dir()
    dummy = tmp / "dummy_session_start.py"
    dummy.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    orig_script = pc._SESSION_START_SCRIPT
    orig_marker = pc._HEALER_SPAWN_MARKER
    orig_log = pc._HEALER_LOG
    try:
        pc._SESSION_START_SCRIPT = dummy
        pc._HEALER_SPAWN_MARKER = tmp / "healer-spawned.json"
        pc._HEALER_LOG = tmp / "healer.log"

        first = pc.spawn_server_healer(cwd=str(tmp))
        second = pc.spawn_server_healer(cwd=str(tmp))  # immediately after -- within cooldown
        assert first is True
        assert second is False
    finally:
        pc._SESSION_START_SCRIPT = orig_script
        pc._HEALER_SPAWN_MARKER = orig_marker
        pc._HEALER_LOG = orig_log
        shutil.rmtree(tmp, ignore_errors=True)


def test_cooldown_allows_a_spawn_once_expired():
    tmp = _tmp_dir()
    dummy = tmp / "dummy_session_start.py"
    dummy.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    orig_script = pc._SESSION_START_SCRIPT
    orig_marker = pc._HEALER_SPAWN_MARKER
    orig_log = pc._HEALER_LOG
    orig_cooldown = pc._HEALER_SPAWN_COOLDOWN_SECONDS
    try:
        pc._SESSION_START_SCRIPT = dummy
        pc._HEALER_SPAWN_MARKER = tmp / "healer-spawned.json"
        pc._HEALER_LOG = tmp / "healer.log"
        pc._HEALER_SPAWN_COOLDOWN_SECONDS = 0.2

        first = pc.spawn_server_healer(cwd=str(tmp))
        time.sleep(0.3)
        second = pc.spawn_server_healer(cwd=str(tmp))
        assert first is True
        assert second is True
    finally:
        pc._SESSION_START_SCRIPT = orig_script
        pc._HEALER_SPAWN_MARKER = orig_marker
        pc._HEALER_LOG = orig_log
        pc._HEALER_SPAWN_COOLDOWN_SECONDS = orig_cooldown
        shutil.rmtree(tmp, ignore_errors=True)


def test_cooldown_claim_is_atomic_not_read_then_write():
    # Regression: the original implementation read the marker, decided to
    # proceed, and only wrote it back much later -- a TOCTOU race where two
    # concurrent callers could both observe "cooldown expired" and both
    # proceed. _claim_healer_cooldown must claim atomically: calling it twice
    # in a row with no marker present must yield exactly one winner.
    tmp = _tmp_dir()
    marker = tmp / "healer-spawned.json"
    orig_marker = pc._HEALER_SPAWN_MARKER
    try:
        pc._HEALER_SPAWN_MARKER = marker
        now = time.time()
        first = pc._claim_healer_cooldown(now)
        second = pc._claim_healer_cooldown(now)
        assert first is True
        assert second is False
    finally:
        pc._HEALER_SPAWN_MARKER = orig_marker
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
    sys.exit(1 if failures else 0)
