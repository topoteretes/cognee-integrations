"""Unit tests for _plugin_common._rotate_log_if_large.

Why this exists: hook.log, healer.log, bootstrap.log, watcher.log, exit-watcher.log,
and activity.log were all plain-append, never-rotated logs, discovered live at
healer.log=1.75GB and bootstrap.log=272MB (2026-07-18) -- grown unbounded since this
machine's earliest use of the plugin, both because nothing ever capped them and
because each `tail`/`grep` against them had gotten noticeably slow. Every write site
now checks _rotate_log_if_large() immediately before opening for append.

Run: python integrations/claude-code/tests/test_log_rotation.py (or via pytest).
"""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402


def _tmp_dir():
    return pathlib.Path(tempfile.mkdtemp())


def test_small_log_is_left_untouched():
    tmp = _tmp_dir()
    log = tmp / "small.log"
    log.write_text("x" * 100, encoding="utf-8")
    pc._rotate_log_if_large(log, max_bytes=1000)
    assert log.read_text(encoding="utf-8") == "x" * 100


def test_oversized_log_is_truncated():
    tmp = _tmp_dir()
    log = tmp / "big.log"
    log.write_text("x" * 2000, encoding="utf-8")
    pc._rotate_log_if_large(log, max_bytes=1000)
    assert log.read_text(encoding="utf-8") == ""
    assert log.exists()  # truncated in place, not deleted -- the next append still works


def test_log_exactly_at_the_threshold_is_not_rotated():
    # Rotation triggers on strictly GREATER THAN max_bytes -- a log that has just
    # reached the cap (common right after a write) isn't wastefully truncated on the
    # very next check before it's actually grown past it.
    tmp = _tmp_dir()
    log = tmp / "exact.log"
    log.write_text("x" * 1000, encoding="utf-8")
    pc._rotate_log_if_large(log, max_bytes=1000)
    assert log.read_text(encoding="utf-8") == "x" * 1000


def test_missing_log_is_a_noop():
    tmp = _tmp_dir()
    log = tmp / "does_not_exist.log"
    pc._rotate_log_if_large(log, max_bytes=1000)  # must not raise
    assert not log.exists()


def test_default_max_bytes_is_used_when_not_specified():
    tmp = _tmp_dir()
    log = tmp / "default.log"
    orig = pc._LOG_MAX_BYTES
    try:
        pc._LOG_MAX_BYTES = 50
        log.write_text("x" * 100, encoding="utf-8")
        pc._rotate_log_if_large(log)
        assert log.read_text(encoding="utf-8") == ""
    finally:
        pc._LOG_MAX_BYTES = orig


def test_rotation_check_never_raises_on_a_locked_or_unwritable_path():
    # Best-effort per the function's own contract -- a rotation FAILURE must never
    # block the write that's about to happen at the call site. Simulate a write
    # failure by pointing at a directory (write_text on a directory path raises).
    tmp = _tmp_dir()
    a_directory = tmp / "not_a_file"
    a_directory.mkdir()
    pc._rotate_log_if_large(a_directory, max_bytes=0)  # size(dir) > 0 is platform-dependent; must not raise


def test_hook_log_rotates_when_oversized():
    tmp = _tmp_dir()
    log = tmp / "hook.log"
    log.write_text("x" * 2000, encoding="utf-8")
    orig_log = pc._HOOK_LOG
    orig_max = pc._LOG_MAX_BYTES
    try:
        pc._HOOK_LOG = log
        pc._LOG_MAX_BYTES = 1000
        pc.hook_log("some_event", {"k": "v"})
    finally:
        pc._HOOK_LOG = orig_log
        pc._LOG_MAX_BYTES = orig_max
    content = log.read_text(encoding="utf-8")
    assert "x" * 2000 not in content  # the old bloat is gone
    assert "some_event" in content  # the new entry landed


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
