"""Unit tests for _plugin_common.server_usable (#298).

``server_usable`` is the write-hook readiness check: a fresh ready marker
short-circuits; a stale marker triggers ONE bounded /health probe that
re-marks ready on success (so the marker stays fresh through long agent turns
and the warmup buffer stops filling against a healthy server); a failed probe
is memoized so a genuinely-down server costs one probe per backoff window,
not one per tool call.

Run: python integrations/codex/tests/test_server_usable.py (or via pytest).
"""

import json
import pathlib
import sys
import tempfile

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts")
)

import _plugin_common as pc  # noqa: E402

_URL = "http://localhost:8011"


def _drive(fn):
    """Run fn(calls) with marker/memo state and network helpers stubbed.

    ``calls`` records every stubbed side effect so tests can assert on probe
    count and marker refreshes. State is restored afterwards so the suite
    stays order-independent under pytest.
    """
    saved = {
        k: getattr(pc, k)
        for k in (
            "server_ready_hint",
            "server_health_ok",
            "mark_server_ready",
            "_PROBE_FAIL_MEMO",
            "hook_log",
        )
    }
    calls = {"probes": 0, "marked": 0, "hint": False, "healthy": False}
    with tempfile.TemporaryDirectory() as tmp:
        pc._PROBE_FAIL_MEMO = pathlib.Path(tmp) / "probe-fail.json"
        pc.hook_log = lambda *a, **k: None
        pc.server_ready_hint = lambda url="": calls["hint"]

        def _probe(url="", timeout=1.0):
            calls["probes"] += 1
            return calls["healthy"]

        def _mark(url, version=""):
            calls["marked"] += 1

        pc.server_health_ok = _probe
        pc.mark_server_ready = _mark
        try:
            return fn(calls)
        finally:
            for k, v in saved.items():
                setattr(pc, k, v)


def test_fresh_hint_short_circuits():
    """A fresh ready marker returns True without any network probe."""

    def _t(calls):
        calls["hint"] = True
        assert pc.server_usable(_URL) is True
        assert calls["probes"] == 0

    _drive(_t)


def test_stale_hint_healthy_probe_refreshes_marker():
    """Stale marker + healthy server: one probe, marker re-marked, True."""

    def _t(calls):
        calls["healthy"] = True
        assert pc.server_usable(_URL) is True
        assert calls["probes"] == 1
        assert calls["marked"] == 1

    _drive(_t)


def test_failed_probe_returns_false_and_memoizes():
    """Stale marker + down server: False, and the failure memo suppresses
    re-probing inside the backoff window (one probe total, not two)."""

    def _t(calls):
        assert pc.server_usable(_URL) is False
        assert calls["probes"] == 1
        assert pc._PROBE_FAIL_MEMO.exists()
        # Second call inside the backoff window: no new probe.
        assert pc.server_usable(_URL) is False
        assert calls["probes"] == 1

    _drive(_t)


def test_expired_memo_probes_again_and_recovers():
    """An expired failure memo probes again; success clears the memo."""

    def _t(calls):
        assert pc.server_usable(_URL) is False
        assert calls["probes"] == 1
        # Age the memo past the backoff window, then bring the server up.
        memo = json.loads(pc._PROBE_FAIL_MEMO.read_text(encoding="utf-8"))
        memo["failed_at"] = memo["failed_at"] - pc._PROBE_FAIL_BACKOFF_SECONDS - 1
        pc._PROBE_FAIL_MEMO.write_text(json.dumps(memo), encoding="utf-8")
        calls["healthy"] = True
        assert pc.server_usable(_URL) is True
        assert calls["probes"] == 2
        assert calls["marked"] == 1
        assert not pc._PROBE_FAIL_MEMO.exists()

    _drive(_t)


def test_corrupt_memo_fails_open_to_probe():
    """A garbage memo file must not wedge the check: it probes normally."""

    def _t(calls):
        pc._PROBE_FAIL_MEMO.write_text("not json", encoding="utf-8")
        calls["healthy"] = True
        assert pc.server_usable(_URL) is True
        assert calls["probes"] == 1

    _drive(_t)


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
