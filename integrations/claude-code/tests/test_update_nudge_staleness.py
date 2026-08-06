"""The 'update available' nudge must disappear as soon as the update is applied.

The update marker is a snapshot written by the background check in the idle
watcher (≤ hourly). Nothing rewrites it when the plugin is actually updated, so
between the update and the next check the marker still says
``update_available: true`` with a now-stale ``installed_version``. Both surfaces
that read it — the status-line segment and the SessionStart nudge — therefore
compare the marker's ``installed_version`` against the version *running*, and
suppress the nudge on a mismatch.

The running-version comparison alone cannot clear the nudge in the session the
update happened in: installs are version-pinned dirs, and the old copy keeps
running (and rendering) until a restart re-points the status line. Claude Code's
install registry (``installed_plugins.json``) IS rewritten immediately, so both
surfaces also suppress when it records a version at or past the marker's
``latest_version`` — that is what makes the nudge disappear mid-session.

Run: `python integrations/claude-code/tests/test_update_nudge_staleness.py` (or via pytest).
"""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def _load(module_name: str, filename: str):
    """Import a script by path (some filenames are not importable identifiers)."""
    sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(module_name, _SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: pathlib.Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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


# --- status-line segment ------------------------------------------------------


def _segment_with(marker: dict, registry=None) -> str:
    """Render just the update segment against temp marker/registry files.

    The install-registry path is always redirected (to nothing, unless
    ``registry`` is given) so a real ~/.claude/plugins/installed_plugins.json
    on the machine running the tests cannot leak in.
    """
    sl = _load("sl_staleness", "cognee_statusline_render.py")
    tmp = pathlib.Path(tempfile.mkdtemp())
    saved = os.environ.pop("COGNEE_UPDATE_CHECK", None)
    try:
        sl._UPDATE_CHECK_PATH = tmp / "update-check.json"
        sl._INSTALLED_PLUGINS_PATH = tmp / "installed_plugins.json"
        _write(sl._UPDATE_CHECK_PATH, marker)
        if registry is not None:
            _write(sl._INSTALLED_PLUGINS_PATH, registry)
        return sl._update_segment()
    finally:
        if saved is not None:
            os.environ["COGNEE_UPDATE_CHECK"] = saved


def test_segment_shows_when_marker_matches_running_version():
    """Baseline: a marker written by the running version still nudges."""
    sl = _load("sl_staleness", "cognee_statusline_render.py")
    running = sl._running_plugin_version()
    assert running, "expected a readable running version from the repo layout"

    out = _segment_with(_marker(running, "99.0.0"))
    assert "update available" in out, repr(out)
    assert f"{running}→99.0.0" in out, repr(out)


def test_segment_hidden_once_running_version_moved_past_marker():
    """The update landed: marker's installed_version is no longer what runs."""
    out = _segment_with(_marker("0.0.1", "99.0.0"))
    assert out == "", repr(out)


def test_segment_hidden_once_registry_records_latest():
    """Mid-session update: the old copy still renders, but the registry moved."""
    sl = _load("sl_staleness", "cognee_statusline_render.py")
    running = sl._running_plugin_version()
    out = _segment_with(_marker(running, "99.0.0"), registry=_registry("99.0.0"))
    assert out == "", repr(out)


def test_segment_hidden_when_registry_moved_past_latest():
    sl = _load("sl_staleness", "cognee_statusline_render.py")
    running = sl._running_plugin_version()
    out = _segment_with(_marker(running, "99.0.0"), registry=_registry("99.0.1"))
    assert out == "", repr(out)


def test_segment_still_shown_while_registry_behind_latest():
    """No update yet: the registry records the same old version that runs."""
    sl = _load("sl_staleness", "cognee_statusline_render.py")
    running = sl._running_plugin_version()
    out = _segment_with(_marker(running, "99.0.0"), registry=_registry(running))
    assert "update available" in out, repr(out)


def test_segment_malformed_registry_is_ignored():
    """A broken registry must not disable the nudge."""
    sl = _load("sl_staleness", "cognee_statusline_render.py")
    running = sl._running_plugin_version()
    for payload in ({}, {"plugins": []}, {"plugins": {"cognee-memory@cognee": [{}]}}):
        out = _segment_with(_marker(running, "99.0.0"), registry=payload)
        assert "update available" in out, (payload, out)


def test_running_version_matches_plugin_manifest():
    """The renderer resolves its own version from its own location, not the env."""
    sl = _load("sl_staleness", "cognee_statusline_render.py")
    manifest = json.loads(
        (_SCRIPTS.parent / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert sl._running_plugin_version() == manifest["version"]


# --- SessionStart nudge (shared read used by _apply_update_nudge) -------------


def _status_with(marker: dict, registry=None) -> dict:
    common = _load("common_staleness", "_plugin_common.py")
    tmp = pathlib.Path(tempfile.mkdtemp())
    saved = os.environ.pop("COGNEE_UPDATE_CHECK", None)
    try:
        common._UPDATE_CHECK_FILE = tmp / "update-check.json"
        common._INSTALLED_PLUGINS_FILE = tmp / "installed_plugins.json"
        _write(common._UPDATE_CHECK_FILE, marker)
        if registry is not None:
            _write(common._INSTALLED_PLUGINS_FILE, registry)
        return common.read_update_status()
    finally:
        if saved is not None:
            os.environ["COGNEE_UPDATE_CHECK"] = saved


def test_read_update_status_returns_marker_when_current():
    common = _load("common_staleness", "_plugin_common.py")
    running = common._installed_plugin_version()
    assert running, "expected a readable installed version from the repo layout"

    status = _status_with(_marker(running, "99.0.0"))
    assert status.get("update_available") is True
    assert status.get("latest_version") == "99.0.0"


def test_read_update_status_suppressed_after_update():
    assert _status_with(_marker("0.0.1", "99.0.0")) == {}


def test_read_update_status_suppressed_once_registry_records_latest():
    """Mid-session update: still running the old copy, but the registry moved."""
    common = _load("common_staleness", "_plugin_common.py")
    running = common._installed_plugin_version()
    status = _status_with(_marker(running, "99.0.0"), registry=_registry("99.0.0"))
    assert status == {}, repr(status)


def test_read_update_status_kept_while_registry_behind_latest():
    common = _load("common_staleness", "_plugin_common.py")
    running = common._installed_plugin_version()
    status = _status_with(_marker(running, "99.0.0"), registry=_registry(running))
    assert status.get("update_available") is True, repr(status)


def test_unreadable_running_version_falls_back_to_marker():
    """A missing plugin.json must not silently disable the nudge entirely."""
    common = _load("common_staleness", "_plugin_common.py")
    tmp = pathlib.Path(tempfile.mkdtemp())
    saved = os.environ.pop("COGNEE_UPDATE_CHECK", None)
    original = common._installed_plugin_version
    try:
        common._UPDATE_CHECK_FILE = tmp / "update-check.json"
        _write(common._UPDATE_CHECK_FILE, _marker("1.0.0", "99.0.0"))
        common._installed_plugin_version = lambda: ""
        status = common.read_update_status()
        assert status.get("installed_version") == "1.0.0", repr(status)
    finally:
        common._installed_plugin_version = original
        if saved is not None:
            os.environ["COGNEE_UPDATE_CHECK"] = saved


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
    print("\nALL PASSED" if not failures else f"\n{failures} FAILED")
    sys.exit(1 if failures else 0)
