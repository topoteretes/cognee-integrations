"""Tests for `_pipeline_health_glyph` (cognee_statusline_render.py) -- the
passive, app-closed-safe mitigation for the pipeline-health sweep (Layer 1):
PushNotification (Layer 2) only fires while Claude Code is open, so this glyph
is what lets a human see a stuck-pipeline finding the instant they next open
any terminal running the plugin. See
docs/KB/pipeline-monitor-notify-policy.md (total_recall/thessary repo) for the
full monitoring design this is one small piece of.

No unittest.mock, matching this test directory's existing convention: the
module-level _PIPELINE_HEALTH_PATH constant is reassigned directly to a tmp
path and restored in `finally`.

Run: python integrations/claude-code/tests/test_statusline_pipeline_health.py
(or via pytest).
"""

import json
import pathlib
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import cognee_statusline_render as sl  # noqa: E402


def _tmp_path():
    return pathlib.Path(tempfile.mkdtemp()) / "pipeline-health.json"


def _iso(delta_seconds=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=delta_seconds)).isoformat()


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ── no file / malformed file → silently empty, never raises ─────────────────

def test_no_file_returns_empty_string():
    tmp = _tmp_path()  # never written
    orig = sl._PIPELINE_HEALTH_PATH
    try:
        sl._PIPELINE_HEALTH_PATH = tmp
        assert sl._pipeline_health_glyph() == ""
    finally:
        sl._PIPELINE_HEALTH_PATH = orig
        shutil.rmtree(tmp.parent, ignore_errors=True)


def test_malformed_json_returns_empty_string():
    tmp = _tmp_path()
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text("not json{{{", encoding="utf-8")
    orig = sl._PIPELINE_HEALTH_PATH
    try:
        sl._PIPELINE_HEALTH_PATH = tmp
        assert sl._pipeline_health_glyph() == ""
    finally:
        sl._PIPELINE_HEALTH_PATH = orig
        shutil.rmtree(tmp.parent, ignore_errors=True)


# ── staleness gate ────────────────────────────────────────────────────────

def test_stale_file_returns_empty_even_if_it_would_otherwise_warn():
    tmp = _tmp_path()
    _write(tmp, {
        "generated_at": _iso(delta_seconds=sl._PIPELINE_HEALTH_STALE_SECONDS + 60),
        "server": {"up": True},
        "summary": {"total_open": 1, "worst_classification": "critical",
                    "by_classification": {"warn": 0, "alert": 0, "critical": 1}},
    })
    orig = sl._PIPELINE_HEALTH_PATH
    try:
        sl._PIPELINE_HEALTH_PATH = tmp
        assert sl._pipeline_health_glyph() == ""
    finally:
        sl._PIPELINE_HEALTH_PATH = orig
        shutil.rmtree(tmp.parent, ignore_errors=True)


def test_missing_generated_at_returns_empty():
    tmp = _tmp_path()
    _write(tmp, {"server": {"up": True}, "summary": {}})
    orig = sl._PIPELINE_HEALTH_PATH
    try:
        sl._PIPELINE_HEALTH_PATH = tmp
        assert sl._pipeline_health_glyph() == ""
    finally:
        sl._PIPELINE_HEALTH_PATH = orig
        shutil.rmtree(tmp.parent, ignore_errors=True)


# ── clean state → empty ──────────────────────────────────────────────────

def test_fresh_clean_state_returns_empty():
    tmp = _tmp_path()
    _write(tmp, {
        "generated_at": _iso(),
        "server": {"up": True},
        "summary": {"total_open": 3, "worst_classification": "ok",
                    "by_classification": {"warn": 0, "alert": 0, "critical": 0}},
    })
    orig = sl._PIPELINE_HEALTH_PATH
    try:
        sl._PIPELINE_HEALTH_PATH = tmp
        assert sl._pipeline_health_glyph() == ""
    finally:
        sl._PIPELINE_HEALTH_PATH = orig
        shutil.rmtree(tmp.parent, ignore_errors=True)


def test_warn_only_returns_empty_never_pushed_never_shown():
    """Matches the notify-policy doc: bare warn is tracked, never surfaced."""
    tmp = _tmp_path()
    _write(tmp, {
        "generated_at": _iso(),
        "server": {"up": True},
        "summary": {"total_open": 1, "worst_classification": "warn",
                    "by_classification": {"warn": 1, "alert": 0, "critical": 0}},
    })
    orig = sl._PIPELINE_HEALTH_PATH
    try:
        sl._PIPELINE_HEALTH_PATH = tmp
        assert sl._pipeline_health_glyph() == ""
    finally:
        sl._PIPELINE_HEALTH_PATH = orig
        shutil.rmtree(tmp.parent, ignore_errors=True)


# ── real findings → glyph shown ──────────────────────────────────────────

def test_server_down_takes_priority_and_shows_its_own_glyph():
    tmp = _tmp_path()
    _write(tmp, {
        "generated_at": _iso(),
        "server": {"up": False},
        "summary": {"total_open": 0, "worst_classification": "ok",
                    "by_classification": {"warn": 0, "alert": 0, "critical": 0}},
    })
    orig = sl._PIPELINE_HEALTH_PATH
    try:
        sl._PIPELINE_HEALTH_PATH = tmp
        assert sl._pipeline_health_glyph() == "⚠ server-down "
    finally:
        sl._PIPELINE_HEALTH_PATH = orig
        shutil.rmtree(tmp.parent, ignore_errors=True)


def test_alert_classification_shows_stuck_count():
    tmp = _tmp_path()
    _write(tmp, {
        "generated_at": _iso(),
        "server": {"up": True},
        "summary": {"total_open": 5, "worst_classification": "alert",
                    "by_classification": {"warn": 1, "alert": 2, "critical": 0}},
    })
    orig = sl._PIPELINE_HEALTH_PATH
    try:
        sl._PIPELINE_HEALTH_PATH = tmp
        assert sl._pipeline_health_glyph() == "⚠ 3 pipeline(s) stuck "
    finally:
        sl._PIPELINE_HEALTH_PATH = orig
        shutil.rmtree(tmp.parent, ignore_errors=True)


def test_critical_classification_shows_stuck_count():
    tmp = _tmp_path()
    _write(tmp, {
        "generated_at": _iso(),
        "server": {"up": True},
        "summary": {"total_open": 2, "worst_classification": "critical",
                    "by_classification": {"warn": 0, "alert": 0, "critical": 1}},
    })
    orig = sl._PIPELINE_HEALTH_PATH
    try:
        sl._PIPELINE_HEALTH_PATH = tmp
        assert sl._pipeline_health_glyph() == "⚠ 1 pipeline(s) stuck "
    finally:
        sl._PIPELINE_HEALTH_PATH = orig
        shutil.rmtree(tmp.parent, ignore_errors=True)


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
