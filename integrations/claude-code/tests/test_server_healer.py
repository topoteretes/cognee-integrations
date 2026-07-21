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


# ── 2026-07-17/18 fix: inflight-worker gating (the retry-cascade bug) ──────────


def test_inflight_worker_blocks_a_new_spawn_even_after_cooldown_expires():
    # The exact incident this fixes: the old cooldown (elapsed-time only) let a new
    # spawn through the moment the SHORT cooldown expired, even while a PREVIOUS
    # worker was still genuinely alive and inside its own much-longer wait-for-health
    # window -- causing spawns to stack for hours against a server that could never
    # become healthy. A worker that's still alive must block a new spawn even once
    # the plain cooldown has elapsed.
    tmp = _tmp_dir()
    dummy = tmp / "dummy_session_start.py"
    dummy.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    orig_script = pc._SESSION_START_SCRIPT
    orig_marker = pc._HEALER_SPAWN_MARKER
    orig_log = pc._HEALER_LOG
    orig_cooldown = pc._HEALER_SPAWN_COOLDOWN_SECONDS
    orig_stale = pc._HEALER_INFLIGHT_STALE_SECONDS
    proc_to_cleanup = None
    try:
        pc._SESSION_START_SCRIPT = dummy
        pc._HEALER_SPAWN_MARKER = tmp / "healer-spawned.json"
        pc._HEALER_LOG = tmp / "healer.log"
        pc._HEALER_SPAWN_COOLDOWN_SECONDS = 0.2  # short, so it expires well before the worker exits
        pc._HEALER_INFLIGHT_STALE_SECONDS = 30.0  # long, so staleness doesn't mask this

        first = pc.spawn_server_healer(cwd=str(tmp))
        assert first is True
        marker = json.loads(pc._HEALER_SPAWN_MARKER.read_text(encoding="utf-8"))
        proc_to_cleanup = marker.get("pid")

        time.sleep(0.4)  # cooldown (0.2s) has now expired, but the dummy worker sleeps 5s
        second = pc.spawn_server_healer(cwd=str(tmp))
        assert second is False, "a new spawn must be blocked while the previous worker is still alive"
    finally:
        pc._SESSION_START_SCRIPT = orig_script
        pc._HEALER_SPAWN_MARKER = orig_marker
        pc._HEALER_LOG = orig_log
        pc._HEALER_SPAWN_COOLDOWN_SECONDS = orig_cooldown
        pc._HEALER_INFLIGHT_STALE_SECONDS = orig_stale
        if proc_to_cleanup:
            try:
                os.kill(int(proc_to_cleanup), 9)
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def test_inflight_check_unblocks_immediately_once_worker_exits():
    # Crash-safety requirement: if the spawned worker dies/exits EARLY (crash, or in
    # this test's case a script that exits fast), a new spawn must be allowed right
    # away -- not blocked for the rest of the (possibly very long) deadline window.
    tmp = _tmp_dir()
    dummy = tmp / "dummy_session_start.py"
    dummy.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")  # exits ~immediately
    orig_script = pc._SESSION_START_SCRIPT
    orig_marker = pc._HEALER_SPAWN_MARKER
    orig_log = pc._HEALER_LOG
    orig_cooldown = pc._HEALER_SPAWN_COOLDOWN_SECONDS
    orig_stale = pc._HEALER_INFLIGHT_STALE_SECONDS
    try:
        pc._SESSION_START_SCRIPT = dummy
        pc._HEALER_SPAWN_MARKER = tmp / "healer-spawned.json"
        pc._HEALER_LOG = tmp / "healer.log"
        pc._HEALER_SPAWN_COOLDOWN_SECONDS = 0.05
        pc._HEALER_INFLIGHT_STALE_SECONDS = 300.0  # long -- must NOT be why the second call succeeds

        first = pc.spawn_server_healer(cwd=str(tmp))
        assert first is True
        time.sleep(0.3)  # let the dummy process actually exit, and let the short cooldown pass
        second = pc.spawn_server_healer(cwd=str(tmp))
        assert second is True, "a dead worker's PID must not block a new spawn, regardless of the stale-window setting"
    finally:
        pc._SESSION_START_SCRIPT = orig_script
        pc._HEALER_SPAWN_MARKER = orig_marker
        pc._HEALER_LOG = orig_log
        pc._HEALER_SPAWN_COOLDOWN_SECONDS = orig_cooldown
        pc._HEALER_INFLIGHT_STALE_SECONDS = orig_stale
        shutil.rmtree(tmp, ignore_errors=True)


def test_healer_worker_inflight_treats_missing_pid_field_as_not_inflight():
    # Backward compatibility: a marker written by pre-fix code (or by the placeholder
    # write inside _claim_healer_cooldown itself) has pid=0 / no pid -- must never be
    # treated as "something is inflight", or the very first claim would deadlock.
    tmp = _tmp_dir()
    marker = tmp / "healer-spawned.json"
    marker.write_text(json.dumps({"ts": time.time()}), encoding="utf-8")  # no "pid" key at all
    orig_marker = pc._HEALER_SPAWN_MARKER
    try:
        pc._HEALER_SPAWN_MARKER = marker
        assert pc._healer_worker_inflight(time.time()) is False
    finally:
        pc._HEALER_SPAWN_MARKER = orig_marker
        shutil.rmtree(tmp, ignore_errors=True)


def test_healer_worker_inflight_ignores_a_marker_past_the_stale_window():
    # Even if the recorded pid happened to still resolve to a live (unrelated,
    # PID-reused) process, a marker older than _HEALER_INFLIGHT_STALE_SECONDS must
    # never block forever -- age is an unconditional escape hatch.
    tmp = _tmp_dir()
    marker = tmp / "healer-spawned.json"
    # Use THIS test process's own pid -- guaranteed alive -- to isolate the age check
    # from any pid-liveness variable.
    marker.write_text(json.dumps({"ts": time.time() - 999999, "pid": os.getpid()}), encoding="utf-8")
    orig_marker = pc._HEALER_SPAWN_MARKER
    orig_stale = pc._HEALER_INFLIGHT_STALE_SECONDS
    try:
        pc._HEALER_SPAWN_MARKER = marker
        pc._HEALER_INFLIGHT_STALE_SECONDS = 300.0
        assert pc._healer_worker_inflight(time.time()) is False
    finally:
        pc._HEALER_SPAWN_MARKER = orig_marker
        pc._HEALER_INFLIGHT_STALE_SECONDS = orig_stale
        shutil.rmtree(tmp, ignore_errors=True)


def test_healer_worker_inflight_true_for_a_live_recent_pid():
    tmp = _tmp_dir()
    marker = tmp / "healer-spawned.json"
    marker.write_text(json.dumps({"ts": time.time(), "pid": os.getpid()}), encoding="utf-8")
    orig_marker = pc._HEALER_SPAWN_MARKER
    try:
        pc._HEALER_SPAWN_MARKER = marker
        assert pc._healer_worker_inflight(time.time()) is True
    finally:
        pc._HEALER_SPAWN_MARKER = orig_marker
        shutil.rmtree(tmp, ignore_errors=True)


def test_corrupt_marker_self_heals_instead_of_permanently_deadlocking():
    # 2026-07-18 adversarial-test finding: a marker that EXISTS but contains unparseable JSON
    # (e.g. a torn write from a process killed mid-write during the exclusive-create claim
    # path) used to fall into `_claim_healer_cooldown`'s generic `except Exception: return
    # False` -- which never repairs the file, so EVERY future call hits the identical
    # exception forever. That would permanently disable the self-healing mechanism this
    # whole fix exists to provide, until a human manually deletes the marker -- exactly the
    # class of silent, permanent regression the fix itself was built to prevent. Reproduced
    # live against the actually-deployed plugin-cache copy before this test was written.
    tmp = _tmp_dir()
    marker = tmp / "healer-spawned.json"
    marker.write_text('{"pid": 123, "ts": ', encoding="utf-8")  # deliberately truncated JSON
    orig_marker = pc._HEALER_SPAWN_MARKER
    try:
        pc._HEALER_SPAWN_MARKER = marker
        now = time.time()
        claimed = pc._claim_healer_cooldown(now)
        assert claimed is True, "a corrupt marker must be repaired and claimed, not permanently refused"
        # The marker must now be valid JSON with a fresh timestamp -- confirms it was actually
        # repaired on disk, not just that the function happened to return True this once.
        repaired = json.loads(marker.read_text(encoding="utf-8"))
        assert repaired["pid"] == 0
        assert abs(repaired["ts"] - now) < 5
        # And a second call afterward must behave normally (respect the cooldown), proving
        # this isn't a one-shot fluke -- the repair actually restored steady-state behavior.
        assert pc._claim_healer_cooldown(time.time()) is False
    finally:
        pc._HEALER_SPAWN_MARKER = orig_marker
        shutil.rmtree(tmp, ignore_errors=True)


def test_corrupt_marker_with_wrong_json_shape_also_self_heals():
    # A different corruption shape: syntactically valid JSON that isn't the expected dict
    # (e.g. `null`, from a write that only got as far as flushing an empty/placeholder value).
    # `raw.get(...)` on a non-dict raises AttributeError, not JSONDecodeError -- must be
    # caught by the same repair path, not slip through to the permanent-deadlock branch.
    tmp = _tmp_dir()
    marker = tmp / "healer-spawned.json"
    marker.write_text("null", encoding="utf-8")
    orig_marker = pc._HEALER_SPAWN_MARKER
    try:
        pc._HEALER_SPAWN_MARKER = marker
        assert pc._claim_healer_cooldown(time.time()) is True
        assert json.loads(marker.read_text(encoding="utf-8"))["pid"] == 0
    finally:
        pc._HEALER_SPAWN_MARKER = orig_marker
        shutil.rmtree(tmp, ignore_errors=True)


def test_pid_alive_correctly_reports_a_genuinely_dead_pid_on_windows():
    # 2026-07-18 adversarial-audit finding (CRITICAL): os.kill(pid, 0) on Windows does NOT
    # detect death -- signal 0 maps to CTRL_C_EVENT there (GenerateConsoleCtrlEvent), not a
    # liveness probe. Confirmed via 15/15 controlled trials (spawn, Popen.wait() to CONFIRM
    # the OS has reaped it, then immediately check) that the old os.kill-based _pid_alive
    # reported a definitively-dead PID as alive every single time. This is the real-process
    # equivalent of test_healer_worker_inflight_ignores_a_marker_past_the_stale_window /
    # test_inflight_check_unblocks_immediately_once_worker_exits above, which use synthetic
    # data and predate this finding -- this test exercises the real OS-level check directly
    # against a real spawned-and-reaped child, which is what actually caught the bug.
    if sys.platform != "win32":
        return  # this bug (and its fix) is Windows-specific; POSIX os.kill(pid,0) is correct
    import subprocess

    mismatches = 0
    trials = 5
    for _ in range(trials):
        p = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        p.wait(timeout=5)
        if pc._pid_alive(p.pid):
            mismatches += 1
    assert mismatches == 0, (
        f"_pid_alive incorrectly reported {mismatches}/{trials} genuinely-dead PIDs as alive "
        "-- the Windows liveness fix has regressed"
    )


def test_claim_race_under_real_concurrency_produces_exactly_one_winner():
    # 2026-07-18 adversarial-audit finding: the "marker exists, cooldown expired" branch of
    # _claim_healer_cooldown used to read-then-later-write with NO lock between the two --
    # forced the exact interleaving with real concurrent threads and got multiple winners.
    # This drives many REAL concurrent threads through the full _claim_healer_cooldown path
    # (not the already-covered "no marker yet" cold-start case) to prove the claim mutex
    # actually serializes the steady-state "marker exists" path too.
    import threading

    tmp = _tmp_dir()
    marker = tmp / "healer-spawned.json"
    lock = tmp / "healer-claim.lock"
    orig_marker = pc._HEALER_SPAWN_MARKER
    orig_lock = pc._HEALER_CLAIM_LOCK
    orig_cooldown = pc._HEALER_SPAWN_COOLDOWN_SECONDS
    try:
        pc._HEALER_SPAWN_MARKER = marker
        pc._HEALER_CLAIM_LOCK = lock
        # A REAL positive cooldown, not 0 -- with 0, "now - last < 0" is false for every
        # claimant regardless of ordering, so even a perfectly-serialized (correctly mutexed)
        # sequence would still show every claimant "winning", making the test unable to tell
        # a working mutex apart from a broken one. ts=0 (epoch) makes the marker read as
        # already-expired for the FIRST claimant; whichever thread's write lands first bumps
        # ts to ~now, which must then correctly block every OTHER thread's read within the
        # same 60s window -- that's the actual property under test.
        pc._HEALER_SPAWN_COOLDOWN_SECONDS = 60.0
        marker.write_text(json.dumps({"ts": 0.0, "pid": 0}), encoding="utf-8")

        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(15)

        def worker():
            barrier.wait()  # force maximum concurrent overlap on the critical section
            now = time.time()
            ok = pc._claim_healer_cooldown(now)
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        wins = sum(1 for r in results if r)
        assert wins == 1, f"race: {wins} of 15 concurrent claimants won simultaneously (expected exactly 1)"
    finally:
        pc._HEALER_SPAWN_MARKER = orig_marker
        pc._HEALER_CLAIM_LOCK = orig_lock
        pc._HEALER_SPAWN_COOLDOWN_SECONDS = orig_cooldown
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
