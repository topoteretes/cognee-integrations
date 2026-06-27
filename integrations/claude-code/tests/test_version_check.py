"""Unit tests for the plugin-update check (version_check.py) and the status-line badge.

Covers:
  * version comparison (only a strictly-newer published version triggers an update);
  * the TTL gate (a recent check is a no-op — no network);
  * a network failure preserves the last known 'latest' instead of clearing it;
  * the status line renders the badge only when update_available is set.

Run: python integrations/claude-code/tests/test_version_check.py (or via pytest).
"""

import json
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import cognee_statusline_render as sl  # noqa: E402
import version_check as vc  # noqa: E402


def _tmp_state():
    return pathlib.Path(tempfile.mkdtemp()) / "update-check.json"


# ---- version comparison ----


def test_is_newer():
    assert vc.is_newer("0.3.0", "0.2.0")
    assert vc.is_newer("0.2.1", "0.2.0")
    assert vc.is_newer("1.0.0", "0.9.9")
    assert not vc.is_newer("0.2.0", "0.2.0")  # equal → no update
    assert not vc.is_newer("0.1.0", "0.2.0")  # older → no update
    assert not vc.is_newer("", "0.2.0")  # unknown latest → no false badge
    assert not vc.is_newer("0.2.0", "")  # unknown installed → no false badge


def test_parse_tolerates_v_and_prerelease():
    assert vc._parse("v0.3.0") == (0, 3, 0)
    assert vc._parse("1.2.3-rc1") == (1, 2, 3)
    assert vc.is_newer("v1.2.3", "v1.2.2")


# ---- run() behavior ----


def test_run_writes_update_available():
    vc._STATE_PATH = _tmp_state()
    vc._latest_version = lambda timeout: "0.3.0"
    vc._installed_version = lambda root: "0.2.0"
    os.environ.pop("COGNEE_UPDATE_CHECK", None)
    vc.run("/fake/root", force=True)
    rec = json.loads(vc._STATE_PATH.read_text())
    assert rec["update_available"] is True
    assert rec["latest"] == "0.3.0"
    assert rec["installed"] == "0.2.0"


def test_run_no_update_when_equal():
    vc._STATE_PATH = _tmp_state()
    vc._latest_version = lambda timeout: "0.2.0"
    vc._installed_version = lambda root: "0.2.0"
    vc.run("/fake/root", force=True)
    assert json.loads(vc._STATE_PATH.read_text())["update_available"] is False


def test_ttl_gate_skips_when_recent():
    vc._STATE_PATH = _tmp_state()
    vc._STATE_PATH.write_text(json.dumps({"checked_at": time.time(), "latest": "9.9.9"}))
    called = {"n": 0}

    def _boom(timeout):
        called["n"] += 1
        raise AssertionError("should not hit the network within the TTL")

    vc._latest_version = _boom
    vc.run("/fake/root")  # not forced → recent check → skip
    assert called["n"] == 0


def test_network_failure_preserves_previous_latest():
    vc._STATE_PATH = _tmp_state()
    # a prior run found 0.4.0 available
    vc._STATE_PATH.write_text(
        json.dumps({"checked_at": 0, "installed": "0.2.0", "latest": "0.4.0"})
    )
    vc._installed_version = lambda root: "0.2.0"

    def _fail(timeout):
        raise OSError("network down")

    vc._latest_version = _fail
    vc.run("/fake/root", force=True)
    rec = json.loads(vc._STATE_PATH.read_text())
    assert rec["latest"] == "0.4.0"  # preserved, not cleared
    assert rec["update_available"] is True


def test_disabled_is_noop():
    vc._STATE_PATH = _tmp_state()
    os.environ["COGNEE_UPDATE_CHECK"] = "false"
    try:
        vc._latest_version = lambda timeout: "9.9.9"
        vc.run("/fake/root", force=True)
        assert not vc._STATE_PATH.exists()  # nothing written
    finally:
        os.environ.pop("COGNEE_UPDATE_CHECK", None)


# ---- status-line badge ----


def test_statusline_shows_badge_when_update_available():
    p = _tmp_state()
    p.write_text(json.dumps({"update_available": True, "latest": "0.3.0"}))
    sl._UPDATE_CHECK_PATH = p
    os.environ.pop("COGNEE_UPDATE_CHECK", None)
    suffix = sl._update_suffix()
    assert "⬆" in suffix and "0.3.0" in suffix


def test_statusline_no_badge_when_up_to_date():
    p = _tmp_state()
    p.write_text(json.dumps({"update_available": False, "latest": "0.2.0"}))
    sl._UPDATE_CHECK_PATH = p
    assert sl._update_suffix() == ""


def test_statusline_badge_respects_opt_out():
    p = _tmp_state()
    p.write_text(json.dumps({"update_available": True, "latest": "0.3.0"}))
    sl._UPDATE_CHECK_PATH = p
    os.environ["COGNEE_UPDATE_CHECK"] = "off"
    try:
        assert sl._update_suffix() == ""
    finally:
        os.environ.pop("COGNEE_UPDATE_CHECK", None)


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
