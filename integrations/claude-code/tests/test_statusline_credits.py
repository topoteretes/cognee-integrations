"""Tests for `_credits_segment` (cognee_statusline_render.py) — the cloud credits
balance + last-operation cost in the Claude Code status line (SDK-355).

Rendering contract:

  * shape: ` · credits: $<n>.<nn>` in green (red when the balance is negative,
    formatted `-$3.50`), optionally followed by a faint ` · last <op> ~$<n>.<nn>`,
    both reset so no color bleeds into the rest of the bar;
  * gated: cloud mode only, fresh marker only (`_CREDITS_STALE_SECONDS`), matching
    base_url only, `COGNEE_STATUSLINE_CREDITS=off` hides it;
  * a missing/malformed/balance-less marker renders nothing and never raises.

No unittest.mock, matching this test directory's convention: module-level paths
and env are reassigned and restored in `finally`/context managers.

Run: python integrations/claude-code/tests/test_statusline_credits.py (or via pytest).
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

import cognee_statusline_render as sl  # noqa: E402

_TENANT_ID = "f8c21da4-6674-4cc5-bc56-de5e93db881d"
_CLOUD_URL = f"https://tenant-{_TENANT_ID}.aws.cognee.ai"

_GREEN = "\033[32m"
_RED = "\033[31m"
_FAINT = "\033[2m"
_RESET = "\033[0m"


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
    """Tenant-keyed map with a single entry for the active tenant."""
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


# ── nothing to show ─────────────────────────────────────────────────────────


def test_no_marker_renders_nothing():
    with _Credits():
        assert sl._credits_segment() == ""


def test_malformed_marker_renders_nothing():
    with _Credits("not json{{{"):
        assert sl._credits_segment() == ""


def test_marker_without_balance_renders_nothing():
    with _Credits({_TENANT_ID: {"base_url": _CLOUD_URL, "checked_at": time.time()}}):
        assert sl._credits_segment() == ""


def test_boolean_balance_renders_nothing():
    # bool is an int subclass; True must not render as "$1.00".
    with _Credits(_fresh_marker(remaining_usd=True)):
        assert sl._credits_segment() == ""


def test_local_mode_renders_nothing():
    with _Credits(_fresh_marker(), base_url=None):
        assert sl._credits_segment() == ""


def test_stale_marker_renders_nothing():
    stale = time.time() - sl._CREDITS_STALE_SECONDS - 1
    with _Credits(_fresh_marker(checked_at=stale)):
        assert sl._credits_segment() == ""


def test_other_servers_marker_renders_nothing():
    with _Credits(_fresh_marker(base_url="https://other-tenant.example")):
        assert sl._credits_segment() == ""


def test_selects_own_tenant_among_several():
    """Two terminals on two tenants share the marker file; each renders only
    its own tenant's entry (matched by the base_url binding)."""
    other = _entry(
        remaining_usd=999.99,
        base_url="https://tenant-0b54dcbd-6b52-4b3e-a1dd-9d251e0f31bb.aws.cognee.ai",
        tenant_id="0b54dcbd-6b52-4b3e-a1dd-9d251e0f31bb",
    )
    marker = _fresh_marker()
    marker["0b54dcbd-6b52-4b3e-a1dd-9d251e0f31bb"] = other
    with _Credits(marker):
        seg = sl._credits_segment()
        assert "$14.23" in seg
        assert "999.99" not in seg


def test_old_flat_format_renders_nothing():
    """A pre-map flat marker (scalar top-level fields) must render nothing and
    never raise — it disappears on the first new-format refresh."""
    flat = {"remaining_usd": 14.23, "base_url": _CLOUD_URL, "checked_at": time.time()}
    with _Credits(flat):
        assert sl._credits_segment() == ""


def test_opt_out_env_renders_nothing():
    with _Credits(_fresh_marker()):
        os.environ["COGNEE_STATUSLINE_CREDITS"] = "off"
        assert sl._credits_segment() == ""


# ── rendering ────────────────────────────────────────────────────────────────


def test_balance_renders_green_two_decimals():
    with _Credits(_fresh_marker()):
        assert sl._credits_segment() == f" · {_GREEN}credits: $14.23{_RESET}"


def test_negative_balance_renders_red_with_sign():
    with _Credits(_fresh_marker(remaining_usd=-158.86)):
        assert sl._credits_segment() == f" · {_RED}credits: -$158.86{_RESET}"


def test_last_op_appended_faint():
    marker = _fresh_marker(last_op={"label": "improve", "cost_usd": 0.14, "at": time.time()})
    with _Credits(marker):
        assert sl._credits_segment() == (
            f" · {_GREEN}credits: $14.23{_RESET} {_FAINT}· last improve ~$0.14{_RESET}"
        )


def test_turn_label_renders():
    # The Stop/StopFailure hook attributes the finished turn's spend as "turn".
    marker = _fresh_marker(last_op={"label": "turn", "cost_usd": 0.04, "at": time.time()})
    with _Credits(marker):
        assert sl._credits_segment() == (
            f" · {_GREEN}credits: $14.23{_RESET} {_FAINT}· last turn ~$0.04{_RESET}"
        )


def test_hooks_json_wires_credits_refresh_at_turn_end():
    """The turn-end refresh must stay registered async on Stop AND StopFailure —
    losing either reintroduces the one-prompt lag (or hides errored turns)."""
    hooks_path = _SCRIPTS.parent / "hooks" / "hooks.json"
    spec = json.loads(hooks_path.read_text(encoding="utf-8"))["hooks"]

    def _credits_entries(event):
        return [
            h
            for grp in spec.get(event, [])
            for h in grp.get("hooks", [])
            if "credits-refresh.py" in h.get("command", "")
        ]

    for event in ("Stop", "StopFailure"):
        entries = _credits_entries(event)
        assert entries, f"credits-refresh.py not registered on {event}"
        assert all(h.get("async") is True for h in entries), f"{event} entry must be async"


def test_last_op_without_cost_shows_balance_only():
    marker = _fresh_marker(last_op={"label": "improve"})
    with _Credits(marker):
        assert sl._credits_segment() == f" · {_GREEN}credits: $14.23{_RESET}"


def test_thousands_separator():
    with _Credits(_fresh_marker(remaining_usd=1234.5)):
        assert "credits: $1,234.50" in sl._credits_segment()


def test_segment_position_in_full_line():
    # The credits segment sits between the mode label and the recall counters.
    with _Credits(_fresh_marker()):
        seg = sl._credits_segment()
        assert seg.startswith(" · ")  # composes after `· cloud` with no extra glue


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
