"""Tests for `_credits_segment` (cognee_statusline_render.py, Codex) — the cloud
credits balance + last-operation cost in the Codex status line (SDK-355).

The Codex line is PLAIN TEXT (no ANSI styling, unlike the Claude Code bar):
` · credits: $<n>.<nn>` optionally followed by ` · last <op> ~$<n>.<nn>`.
The marker is a tenant-keyed map; the segment renders only the entry bound to
this session's base_url, only in cloud mode, only while fresh, and renders
nothing (never raising) for missing/malformed/foreign markers.

Run: python integrations/codex/tests/test_statusline_credits.py (or via pytest).
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts")
)

import cognee_statusline_render as sl  # noqa: E402

_TENANT_ID = "f8c21da4-6674-4cc5-bc56-de5e93db881d"
_TENANT_B = "0b54dcbd-6b52-4b3e-a1dd-9d251e0f31bb"
_CLOUD_URL = f"https://tenant-{_TENANT_ID}.aws.cognee.ai"


def _entry(**overrides):
    entry = {
        "remaining_usd": 14.23,
        "spent_usd": 5.77,
        "total_usd": 20.0,
        "base_url": _CLOUD_URL,
        "tenant_id": _TENANT_ID,
        "checked_at": time.time(),
    }
    entry.update(overrides)
    return entry


def _fresh_marker(**overrides):
    return {_TENANT_ID: _entry(**overrides)}


class _Credits:
    """Point the renderer's credits path at a tmp dir and force cloud mode."""

    def __init__(self, payload=None, base_url=_CLOUD_URL):
        self._payload = payload
        self._base_url = base_url

    def __enter__(self):
        self._dir = pathlib.Path(tempfile.mkdtemp())
        self._orig_path = sl._CREDITS_PATH
        self._env = {
            k: os.environ.get(k)
            for k in ("COGNEE_BASE_URL", "COGNEE_LOCAL_API_URL", "COGNEE_STATUSLINE_CREDITS")
        }
        os.environ.pop("COGNEE_STATUSLINE_CREDITS", None)
        os.environ.pop("COGNEE_LOCAL_API_URL", None)
        if self._base_url is None:
            os.environ.pop("COGNEE_BASE_URL", None)  # → local mode
        else:
            os.environ["COGNEE_BASE_URL"] = self._base_url
        sl._CREDITS_PATH = self._dir / "credits.json"
        if self._payload is not None:
            sl._CREDITS_PATH.write_text(
                self._payload if isinstance(self._payload, str) else json.dumps(self._payload),
                encoding="utf-8",
            )
        return self

    def __exit__(self, *_exc):
        sl._CREDITS_PATH = self._orig_path
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self._dir, ignore_errors=True)
        return False


def test_no_marker_renders_nothing():
    with _Credits():
        assert sl._credits_segment() == ""


def test_malformed_marker_renders_nothing():
    with _Credits("not json{{{"):
        assert sl._credits_segment() == ""


def test_local_mode_renders_nothing():
    with _Credits(_fresh_marker(), base_url=None):
        assert sl._credits_segment() == ""


def test_stale_entry_renders_nothing():
    stale = time.time() - sl._CREDITS_STALE_SECONDS - 1
    with _Credits(_fresh_marker(checked_at=stale)):
        assert sl._credits_segment() == ""


def test_foreign_entry_renders_nothing():
    with _Credits(_fresh_marker(base_url="https://other-tenant.example")):
        assert sl._credits_segment() == ""


def test_old_flat_format_renders_nothing():
    flat = {"remaining_usd": 14.23, "base_url": _CLOUD_URL, "checked_at": time.time()}
    with _Credits(flat):
        assert sl._credits_segment() == ""


def test_opt_out_env_renders_nothing():
    with _Credits(_fresh_marker()):
        os.environ["COGNEE_STATUSLINE_CREDITS"] = "off"
        assert sl._credits_segment() == ""


def test_balance_renders_plain_text():
    with _Credits(_fresh_marker()):
        assert sl._credits_segment() == " · credits: $14.23"


def test_negative_balance_renders_with_sign():
    with _Credits(_fresh_marker(remaining_usd=-158.86)):
        assert sl._credits_segment() == " · credits: -$158.86"


def test_last_op_appended():
    marker = _fresh_marker(last_op={"label": "turn", "cost_usd": 0.04, "at": time.time()})
    with _Credits(marker):
        assert sl._credits_segment() == " · credits: $14.23 · last turn ~$0.04"


def test_selects_own_tenant_among_several():
    marker = _fresh_marker()
    marker[_TENANT_B] = _entry(
        remaining_usd=999.99,
        base_url=f"https://tenant-{_TENANT_B}.aws.cognee.ai",
        tenant_id=_TENANT_B,
    )
    with _Credits(marker):
        seg = sl._credits_segment()
        assert "$14.23" in seg
        assert "999.99" not in seg


def test_segment_in_host_status_string():
    with _Credits(_fresh_marker()):
        line = sl.render_status_for_host("host-1")
        assert " · credits: $14.23" in line


def test_hooks_json_wires_credits_refresh_on_stop():
    """Codex has no async hooks (async:true is SKIPPED) and no StopFailure —
    the turn-end refresh must be a plain sync Stop entry with a tight timeout."""
    hooks_path = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "hooks.json"
    spec = json.loads(hooks_path.read_text(encoding="utf-8"))["hooks"]
    entries = [
        h
        for grp in spec.get("Stop", [])
        for h in grp.get("hooks", [])
        if "credits-refresh.py" in h.get("command", "")
    ]
    assert entries, "credits-refresh.py not registered on Stop"
    for h in entries:
        assert "async" not in h, "codex skips async hooks entirely"
        assert isinstance(h.get("timeout"), (int, float)) and h["timeout"] <= 15


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
