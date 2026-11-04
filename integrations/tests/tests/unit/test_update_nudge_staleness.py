"""The update nudge must vanish the moment it stops being true.

Two readers share one contract: `cognee_statusline_render._update_segment` (the
bar) and `_plugin_common.read_update_status` (the SessionStart nudge). Both must
suppress a marker that has gone stale, which happens in two ways:

  1. **the running copy moved past the marker** — the marker's
     `installed_version` is no longer the version actually executing, so the
     update already landed;
  2. **the install registry moved** (claude-code only) —
     `~/.claude/plugins/installed_plugins.json` is rewritten immediately on
     update, so a session still running the old copy can tell the update
     happened mid-session. codex has no plugin registry (grep finds no
     `_INSTALLED_PLUGINS` anywhere in its scripts), so those cases are
     claude-only and its comparison is running-version-only.

A malformed registry must never disable the nudge, and an unreadable running
version must fall back to the marker rather than silently killing it.

Migrated from {claude-code,codex}/tests/test_update_nudge_staleness.py, which
re-exec'd a fresh module copy per call (growing sys.path each time) and, in one
case, read the developer's real registry.
"""

from __future__ import annotations

import json

import pytest
from utils.statusline import write_json


def _marker(installed: str, latest: str) -> dict:
    return {
        "update_available": True,
        "installed_version": installed,
        "latest_version": latest,
        "notified_version": "",
        "last_checked_at": 0,
    }


def _registry(version: str) -> dict:
    """A minimal ~/.claude/plugins/installed_plugins.json payload."""
    return {
        "version": 2,
        "plugins": {"cognee-memory@cognee": [{"scope": "user", "version": version}]},
    }


# ── the status-line segment ────────────────────────────────────────────────


@pytest.fixture
def segment(statusline, monkeypatch):
    """Render just the update segment against marker/registry files in temp HOME."""
    monkeypatch.delenv("COGNEE_UPDATE_CHECK", raising=False)

    def _render(marker: dict, registry=None) -> str:
        write_json(statusline._UPDATE_CHECK_PATH, marker)
        if registry is not None:
            write_json(statusline._INSTALLED_PLUGINS_PATH, registry)
        return statusline._update_segment()

    return _render


@pytest.fixture
def registry_aware(suite, statusline):
    """Skip suites with no install registry to consult."""
    if not hasattr(statusline, "_INSTALLED_PLUGINS_PATH"):
        pytest.skip(f"{suite.name}: no plugin install registry (running-version only)")
    return statusline


def test_segment_shows_when_marker_matches_running_version(statusline, segment):
    """Baseline: a marker written by the running version still nudges."""
    running = statusline._running_plugin_version()
    assert running, "expected a readable running version from the repo layout"

    out = segment(_marker(running, "99.0.0"))
    assert "update available" in out, repr(out)
    assert f"{running}→99.0.0" in out, repr(out)


def test_segment_hidden_once_running_version_moved_past_marker(segment):
    """The update landed: marker's installed_version is no longer what runs."""
    assert segment(_marker("0.0.1", "99.0.0")) == ""


def test_segment_hidden_once_registry_records_latest(registry_aware, segment):
    """Mid-session update: the old copy still renders, but the registry moved."""
    running = registry_aware._running_plugin_version()
    assert segment(_marker(running, "99.0.0"), registry=_registry("99.0.0")) == ""


def test_segment_hidden_when_registry_moved_past_latest(registry_aware, segment):
    running = registry_aware._running_plugin_version()
    assert segment(_marker(running, "99.0.0"), registry=_registry("99.0.1")) == ""


def test_segment_still_shown_while_registry_behind_latest(registry_aware, segment):
    """No update yet: the registry records the same old version that runs."""
    running = registry_aware._running_plugin_version()
    out = segment(_marker(running, "99.0.0"), registry=_registry(running))
    assert "update available" in out, repr(out)


def test_segment_malformed_registry_is_ignored(registry_aware, segment):
    """A broken registry must not disable the nudge."""
    running = registry_aware._running_plugin_version()
    for payload in ({}, {"plugins": []}, {"plugins": {"cognee-memory@cognee": [{}]}}):
        out = segment(_marker(running, "99.0.0"), registry=payload)
        assert "update available" in out, (payload, out)


def test_running_version_matches_plugin_manifest(statusline, suite):
    """The renderer resolves its own version from its own location, not the env."""
    manifest = json.loads(suite.plugin_manifest.read_text(encoding="utf-8"))
    assert statusline._running_plugin_version() == manifest["version"]


# ── the SessionStart nudge (shared read used by _apply_update_nudge) ───────


@pytest.fixture
def status(suite, isolated_modules, monkeypatch):
    """Read the update status against marker/registry files in temp HOME."""
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.delenv("COGNEE_UPDATE_CHECK", raising=False)

    def _read(marker: dict, registry=None) -> dict:
        write_json(common._UPDATE_CHECK_FILE, marker)
        if registry is not None:
            write_json(common._INSTALLED_PLUGINS_FILE, registry)
        return common.read_update_status()

    _read.common = common
    return _read


def test_read_update_status_returns_marker_when_current(status):
    running = status.common._installed_plugin_version()
    assert running, "expected a readable installed version from the repo layout"

    result = status(_marker(running, "99.0.0"))
    assert result.get("update_available") is True
    assert result.get("latest_version") == "99.0.0"


def test_read_update_status_suppressed_after_update(status):
    assert status(_marker("0.0.1", "99.0.0")) == {}


def test_read_update_status_suppressed_once_registry_records_latest(suite, status):
    """Mid-session update: still running the old copy, but the registry moved."""
    if not hasattr(status.common, "_INSTALLED_PLUGINS_FILE"):
        pytest.skip(f"{suite.name}: no plugin install registry (running-version only)")
    running = status.common._installed_plugin_version()
    assert status(_marker(running, "99.0.0"), registry=_registry("99.0.0")) == {}


def test_read_update_status_kept_while_registry_behind_latest(suite, status):
    if not hasattr(status.common, "_INSTALLED_PLUGINS_FILE"):
        pytest.skip(f"{suite.name}: no plugin install registry (running-version only)")
    running = status.common._installed_plugin_version()
    result = status(_marker(running, "99.0.0"), registry=_registry(running))
    assert result.get("update_available") is True, repr(result)


def test_unreadable_running_version_falls_back_to_marker(status, monkeypatch):
    """A missing plugin.json must not silently disable the nudge entirely."""
    monkeypatch.setattr(status.common, "_installed_plugin_version", lambda: "")
    result = status(_marker("1.0.0", "99.0.0"))
    assert result.get("installed_version") == "1.0.0", repr(result)
