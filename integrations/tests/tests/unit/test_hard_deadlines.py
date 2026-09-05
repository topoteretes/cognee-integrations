"""A read that never finishes cannot hold the prompt or interpreter open."""

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest


def test_read_deadline_bounds_wait_and_exits_cleanly(suite, isolated_modules):
    pc = isolated_modules(suite, "_plugin_common")
    scripts = str(Path(pc.__file__).parent)
    code = (
        "import sys,time;sys.path.insert(0,sys.argv[1]);from _deadlines import bounded_call\n"
        "try: bounded_call(lambda: time.sleep(20), .05)\n"
        "except TimeoutError: print('bounded')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, scripts], capture_output=True, text=True, timeout=3
    )
    assert result.returncode == 0 and result.stdout.strip() == "bounded"


def test_recall_applies_elapsed_time_bound(suite, isolated_modules, monkeypatch):
    pc = isolated_modules(suite, "_plugin_common")
    release = threading.Event()
    monkeypatch.setattr(pc, "_json_http_request", lambda *args, **kwargs: release.wait(5))
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            pc.recall_via_http("query", session_id="s", top_k=1, scope=["graph"], timeout=0.05)
        assert time.monotonic() - started < 0.5
    finally:
        release.set()


def test_concurrent_log_rotation_preserves_previous_generation(suite, isolated_modules, tmp_path):
    lf = isolated_modules(suite, "_logfiles")
    log = tmp_path / "test.log"
    log.write_text("z" * 2048, encoding="utf-8")
    scripts = str(Path(lf.__file__).parent)
    code = (
        "import sys;sys.path.insert(0,sys.argv[1]);from _logfiles import append_line;"
        "assert append_line(sys.argv[2],sys.argv[3],cap=1024)"
    )
    writers = [
        subprocess.Popen([sys.executable, "-c", code, scripts, str(log), str(i)]) for i in range(8)
    ]
    for writer in writers:
        assert writer.wait(timeout=5) == 0
    assert lf.rotated_path(log).read_text() == "z" * 2048
    assert set(log.read_text().splitlines()) == {str(i) for i in range(8)}
