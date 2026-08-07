"""Tests for `_pipeline_health_glyph` (cognee_statusline_render.py) — the
`⚠ N pipeline(s) stuck` / `⚠ server-down` warning in the bar.

The marker is written by an external sweep; the renderer only reads it, and must
stay silent unless the finding is FRESH and actually actionable:
  * no file / malformed file → empty, never raises;
  * a stale file (or one with no `generated_at`) → empty, even when its contents
    would otherwise warn;
  * a clean or warn-only summary → empty; bare warn is tracked, never surfaced
    (matches the notify-policy doc);
  * `server.up == False` takes priority and gets its own glyph;
  * alert/critical report the combined stuck count.

claude-code only: codex's renderer has no `_pipeline_health_glyph`.

Migrated from claude-code/tests/test_statusline_pipeline_health.py, which
repeated a save/restore of the path constant in every test — the isolated import
makes that unnecessary.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from utils.statusline import write_json


@pytest.fixture
def sl(suite, statusline):
    if not hasattr(statusline, "_pipeline_health_glyph"):
        pytest.skip(f"{suite.name}: no pipeline-health segment")
    return statusline


def _iso(delta_seconds=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=delta_seconds)).isoformat()


def _health(sl, *, up=True, worst="ok", total_open=0, counts=None, generated_at=_iso):
    """Write a pipeline-health marker in the shape the sweep produces."""
    payload = {
        "server": {"up": up},
        "summary": {
            "total_open": total_open,
            "worst_classification": worst,
            "by_classification": counts or {"warn": 0, "alert": 0, "critical": 0},
        },
    }
    if generated_at is not None:
        payload["generated_at"] = generated_at() if callable(generated_at) else generated_at
    write_json(sl._PIPELINE_HEALTH_PATH, payload)


# ── no file / malformed file → silently empty, never raises ────────────────


def test_no_file_returns_empty_string(sl):
    assert sl._pipeline_health_glyph() == ""


def test_malformed_json_returns_empty_string(sl):
    sl._PIPELINE_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    sl._PIPELINE_HEALTH_PATH.write_text("not json{{{", encoding="utf-8")
    assert sl._pipeline_health_glyph() == ""


# ── staleness gate ────────────────────────────────────────────────────────


def test_stale_file_returns_empty_even_if_it_would_otherwise_warn(sl):
    _health(
        sl,
        worst="critical",
        total_open=1,
        counts={"warn": 0, "alert": 0, "critical": 1},
        generated_at=_iso(sl._PIPELINE_HEALTH_STALE_SECONDS + 60),
    )
    assert sl._pipeline_health_glyph() == ""


def test_missing_generated_at_returns_empty(sl):
    _health(sl, generated_at=None)
    assert sl._pipeline_health_glyph() == ""


# ── clean state → empty ───────────────────────────────────────────────────


def test_fresh_clean_state_returns_empty(sl):
    _health(sl, total_open=3, worst="ok")
    assert sl._pipeline_health_glyph() == ""


def test_warn_only_returns_empty_never_pushed_never_shown(sl):
    """Matches the notify-policy doc: bare warn is tracked, never surfaced."""
    _health(sl, total_open=1, worst="warn", counts={"warn": 1, "alert": 0, "critical": 0})
    assert sl._pipeline_health_glyph() == ""


# ── real findings → glyph shown ───────────────────────────────────────────


def test_server_down_takes_priority_and_shows_its_own_glyph(sl):
    _health(sl, up=False)
    assert sl._pipeline_health_glyph() == "⚠ server-down "


def test_alert_classification_shows_stuck_count(sl):
    _health(sl, total_open=5, worst="alert", counts={"warn": 1, "alert": 2, "critical": 0})
    assert sl._pipeline_health_glyph() == "⚠ 3 pipeline(s) stuck "


def test_critical_classification_shows_stuck_count(sl):
    _health(sl, total_open=2, worst="critical", counts={"warn": 0, "alert": 0, "critical": 1})
    assert sl._pipeline_health_glyph() == "⚠ 1 pipeline(s) stuck "
