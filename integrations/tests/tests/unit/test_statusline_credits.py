"""Tests for `_credits_segment` (cognee_statusline_render.py) — the cloud credits
balance + last-operation cost in the status line (SDK-355).

Rendering contract (shared by all registered suites):

  * shape: ` · credits: $<n>.<nn>` (`-$3.50` when negative), optionally followed
    by ` · last <op> ~$<n>.<nn>`;
  * gated: cloud mode only, fresh marker only (`_CREDITS_STALE_SECONDS`),
    matching base_url only, `COGNEE_STATUSLINE_CREDITS=off` hides it;
  * a missing/malformed/balance-less marker renders nothing and never raises.

Shared assertions read the segment through ``strip_ansi``. Claude Code colours
the balance (green, red when negative) and must not make the cost faint — the
cost is a first-class signal, unlike the recall/saved diagnostics; Codex and
Antigravity stay plain. Those live in the per-suite sections, as does the
genuinely different hooks.json wiring.

Migrated from {claude-code,codex}/tests/test_statusline_credits.py. Four cases
(balance-less marker, boolean balance, last_op without cost, thousands
separator) existed only on the claude side even though codex implements the same
behaviour — parametrizing closes that gap.
"""

from __future__ import annotations

import json
import time

import pytest
from utils.statusline import strip_ansi, write_json

_TENANT_ID = "f8c21da4-6674-4cc5-bc56-de5e93db881d"
_CLOUD_URL = f"https://tenant-{_TENANT_ID}.aws.cognee.ai"
_OTHER_TENANT = "0b54dcbd-6b52-4b3e-a1dd-9d251e0f31bb"

_GREEN = "\033[32m"
_RED = "\033[31m"
_FAINT = "\033[2m"
_RESET = "\033[0m"


@pytest.fixture
def sl(statusline, monkeypatch):
    """The renderer in cloud mode (credits are a cloud-only concept)."""
    monkeypatch.setenv("COGNEE_BASE_URL", _CLOUD_URL)
    return statusline


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


def _marker(sl, payload=None, **overrides):
    """Write the tenant-keyed credits marker (default: one fresh own entry)."""
    if payload is None:
        payload = {_TENANT_ID: _entry(**overrides)}
    if isinstance(payload, str):
        sl._CREDITS_PATH.parent.mkdir(parents=True, exist_ok=True)
        sl._CREDITS_PATH.write_text(payload, encoding="utf-8")
    else:
        write_json(sl._CREDITS_PATH, payload)


# ── nothing to show ────────────────────────────────────────────────────────


def test_no_marker_renders_nothing(sl):
    assert sl._credits_segment() == ""


def test_malformed_marker_renders_nothing(sl):
    _marker(sl, "not json{{{")
    assert sl._credits_segment() == ""


def test_marker_without_balance_renders_nothing(sl):
    _marker(sl, {_TENANT_ID: {"base_url": _CLOUD_URL, "checked_at": time.time()}})
    assert sl._credits_segment() == ""


def test_boolean_balance_renders_nothing(sl):
    # bool is an int subclass; True must not render as "$1.00".
    _marker(sl, remaining_usd=True)
    assert sl._credits_segment() == ""


def test_local_mode_renders_nothing(statusline):
    """No COGNEE_BASE_URL exported => local mode, which has no credits concept."""
    _marker(statusline)
    assert statusline._credits_segment() == ""


def test_stale_marker_renders_nothing(sl):
    _marker(sl, checked_at=time.time() - sl._CREDITS_STALE_SECONDS - 1)
    assert sl._credits_segment() == ""


def test_other_servers_marker_renders_nothing(sl):
    _marker(sl, base_url="https://other-tenant.example")
    assert sl._credits_segment() == ""


def test_old_flat_format_renders_nothing(sl):
    """A pre-map flat marker (scalar top-level fields) must render nothing and
    never raise — it disappears on the first new-format refresh."""
    _marker(sl, {"remaining_usd": 14.23, "base_url": _CLOUD_URL, "checked_at": time.time()})
    assert sl._credits_segment() == ""


def test_opt_out_env_renders_nothing(sl, monkeypatch):
    _marker(sl)
    monkeypatch.setenv("COGNEE_STATUSLINE_CREDITS", "off")
    assert sl._credits_segment() == ""


# ── rendering ──────────────────────────────────────────────────────────────


def test_balance_renders_two_decimals(sl):
    _marker(sl)
    assert strip_ansi(sl._credits_segment()) == " · credits: $14.23"


def test_negative_balance_renders_with_sign(sl):
    _marker(sl, remaining_usd=-158.86)
    assert strip_ansi(sl._credits_segment()) == " · credits: -$158.86"


def test_thousands_separator(sl):
    _marker(sl, remaining_usd=1234.5)
    assert "credits: $1,234.50" in strip_ansi(sl._credits_segment())


def test_last_op_appended(sl):
    _marker(sl, last_op={"label": "improve", "cost_usd": 0.14, "at": time.time()})
    assert strip_ansi(sl._credits_segment()) == " · credits: $14.23 · last improve ~$0.14"


def test_turn_label_renders(sl):
    # The turn-end hook attributes the finished turn's spend as "turn".
    _marker(sl, last_op={"label": "turn", "cost_usd": 0.04, "at": time.time()})
    assert strip_ansi(sl._credits_segment()) == " · credits: $14.23 · last turn ~$0.04"


def test_last_op_without_cost_shows_balance_only(sl):
    _marker(sl, last_op={"label": "improve"})
    assert strip_ansi(sl._credits_segment()) == " · credits: $14.23"


def test_selects_own_tenant_among_several(sl):
    """Two terminals on two tenants share the marker file; each renders only its
    own tenant's entry (matched by the base_url binding)."""
    _marker(
        sl,
        {
            _TENANT_ID: _entry(),
            _OTHER_TENANT: _entry(
                remaining_usd=999.99,
                base_url=f"https://tenant-{_OTHER_TENANT}.aws.cognee.ai",
                tenant_id=_OTHER_TENANT,
            ),
        },
    )
    seg = sl._credits_segment()
    assert "$14.23" in seg
    assert "999.99" not in seg


def test_segment_composes_after_the_mode_label(sl):
    _marker(sl)
    assert sl._credits_segment().startswith(" · ")  # no extra glue needed


# ── colour policy (claude-code only) ───────────────────────────────────────


@pytest.fixture
def styled(suite, sl):
    if not hasattr(sl, "_ok_glyph"):
        pytest.skip(f"{suite.name}: the bar is plain text by design (model context)")
    return sl


def test_balance_is_green(styled):
    _marker(styled)
    assert styled._credits_segment() == f" · {_GREEN}credits: $14.23{_RESET}"


def test_negative_balance_is_red(styled):
    _marker(styled, remaining_usd=-158.86)
    assert styled._credits_segment() == f" · {_RED}credits: -$158.86{_RESET}"


def test_last_op_is_not_faint(styled):
    """The cost is a first-class signal, unlike the recall/saved diagnostics."""
    _marker(styled, last_op={"label": "improve", "cost_usd": 0.14, "at": time.time()})
    seg = styled._credits_segment()
    assert seg == f" · {_GREEN}credits: $14.23{_RESET} · last improve ~$0.14"
    assert _FAINT not in seg, "last-op cost must not be faint"


# ── plain-text guard (codex only) ──────────────────────────────────────────


@pytest.fixture
def plain(suite, sl):
    if hasattr(sl, "_ok_glyph"):
        pytest.skip(f"{suite.name}: the bar is deliberately styled for a terminal")
    return sl


def test_segment_has_no_ansi_escapes(plain):
    _marker(plain, last_op={"label": "improve", "cost_usd": 0.14, "at": time.time()})
    assert "\033" not in plain._credits_segment()


def test_segment_reaches_the_host_status_string(plain):
    """The hook-facing emitter is what feeds the model's context."""
    _marker(plain)
    assert " · credits: $14.23" in plain.render_status_for_host("host-1")


# ── the turn-end refresh stays wired (contracts differ per host) ───────────


def _credits_entries(suite, event):
    spec = json.loads(suite.hooks_json.read_text(encoding="utf-8"))["hooks"]
    return [
        hook
        for group in spec.get(event, [])
        for hook in group.get("hooks", [])
        if "credits-refresh.py" in hook.get("command", "")
    ]


def test_hooks_json_wires_credits_refresh_at_turn_end(suite):
    """claude-code supports async hooks and has StopFailure: losing either
    reintroduces the one-prompt lag (or hides errored turns). codex skips async
    hooks entirely and has no StopFailure, so its entry must be a plain sync
    Stop hook with a tight timeout."""
    if suite.hook_manifest_style == "named":
        pytest.skip(
            f"{suite.name}: named hook manifests are covered by the dedicated contract test"
        )
    entries = _credits_entries(suite, "Stop")
    assert entries, "credits-refresh.py not registered on Stop"

    if suite.has_async_hooks:
        for event in ("Stop", "StopFailure"):
            event_entries = _credits_entries(suite, event)
            assert event_entries, f"credits-refresh.py not registered on {event}"
            assert all(h.get("async") is True for h in event_entries), (
                f"{event} entry must be async"
            )
    else:
        for hook in entries:
            assert "async" not in hook, f"{suite.name} skips async hooks entirely"
            assert isinstance(hook.get("timeout"), (int, float)) and hook["timeout"] <= 15
