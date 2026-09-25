"""Tests for `_credits_segment` (cognee_statusline_render.py) — the cloud credits
balance + last-operation cost in the status line (SDK-355).

Rendering contract (shared by all registered suites):

  * shape: ` · credits: $<n>.<nn>` (`-$3.50` when negative), optionally followed
    by ` · last <op> ~$<n>.<nn>`;
  * gated: cloud mode only, matching base_url only, entry younger than the
    marker's prune horizon (`_CREDITS_MAX_AGE_SECONDS`),
    `COGNEE_STATUSLINE_CREDITS=off` hides it;
  * age: a reading older than `_CREDITS_AGE_HINT_SECONDS` renders with a
    trailing ` (Nm ago)` / ` (Nh ago)` / ` (Nd ago)` hint instead of hiding —
    there is no background poll, so an idle terminal's number is simply old,
    not wrong;
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

_BILLING = "https://platform.cognee.ai/billing"

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


def test_marker_past_prune_horizon_renders_nothing(sl):
    """Older than the writer's own 7-day prune: the entry is about to vanish
    on the next refresh anyway, and a week-old number helps nobody."""
    _marker(sl, checked_at=time.time() - sl._CREDITS_MAX_AGE_SECONDS - 1)
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
    assert strip_ansi(sl._credits_segment()) == f" · credits: -$158.86 · top up: {_BILLING}"


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


# ── age hint (no background poll: old readings show their age) ─────────────


def test_recent_reading_has_no_age_hint(sl):
    _marker(sl, checked_at=time.time() - sl._CREDITS_AGE_HINT_SECONDS + 30)
    assert strip_ansi(sl._credits_segment()) == " · credits: $14.23"


def test_idle_reading_shows_minutes(sl):
    _marker(sl, checked_at=time.time() - 16 * 60 - 5)
    assert strip_ansi(sl._credits_segment()) == " · credits: $14.23 (16m ago)"


def test_idle_reading_shows_hours(sl):
    _marker(sl, checked_at=time.time() - 3 * 3600 - 120)
    assert strip_ansi(sl._credits_segment()) == " · credits: $14.23 (3h ago)"


def test_idle_reading_shows_days(sl):
    _marker(sl, checked_at=time.time() - 2 * 86400 - 3600)
    assert strip_ansi(sl._credits_segment()) == " · credits: $14.23 (2d ago)"


def test_age_hint_follows_last_op(sl):
    """Balance and cost come from the same fetch, so the hint qualifies both."""
    _marker(
        sl,
        checked_at=time.time() - 45 * 60,
        last_op={"label": "turn", "cost_usd": 0.04, "at": time.time() - 45 * 60},
    )
    assert strip_ansi(sl._credits_segment()) == " · credits: $14.23 · last turn ~$0.04 (45m ago)"


def test_age_hint_matches_writer_prune_horizon(suite, sl, isolated_modules):
    """The renderer hides exactly where the writer prunes: one horizon."""
    pc = isolated_modules(suite, "_plugin_common")
    assert sl._CREDITS_MAX_AGE_SECONDS == pc._CREDITS_ENTRY_MAX_AGE_SECONDS


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
    expected = f" · {_RED}credits: -$158.86{_RESET} · {_GREEN}top up: {_BILLING}{_RESET}"
    assert styled._credits_segment() == expected


def test_age_hint_is_faint(styled):
    """The hint is a qualifier, below the number in the visual hierarchy."""
    _marker(styled, checked_at=time.time() - 20 * 60)
    expected = f" · {_GREEN}credits: $14.23{_RESET} {_FAINT}(20m ago){_RESET}"
    assert styled._credits_segment() == expected


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


def test_age_hint_has_no_ansi_escapes(plain):
    _marker(plain, checked_at=time.time() - 20 * 60)
    seg = plain._credits_segment()
    assert seg == " · credits: $14.23 (20m ago)"
    assert "\033" not in seg


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


# ── low balance and 402 refusals ───────────────────────────────────────────
#
# Two signals, two renderings. The balance reading comes from the billing
# overview; the 402 note comes from a billable request the server refused
# ("not enough credits for THIS operation"). A dollar or less is red with the
# top-up link — not zero: the cloud refuses requests with cents still left, so
# zero is never observed. A refusal above a dollar keeps the (green) number and
# adds a red qualifier; a refusal with no balance reading at all (the billing
# fetch itself failed) is the whole segment, with the link.


def _refused(op="recall", age=0.0):
    return {"op": op, "at": time.time() - age}


@pytest.mark.parametrize("remaining, shown", [(1.0, "$1.00"), (0.37, "$0.37"), (0.0, "$0.00")])
def test_a_dollar_or_less_shows_the_top_up_link(sl, remaining, shown):
    _marker(sl, remaining_usd=remaining)
    assert strip_ansi(sl._credits_segment()) == f" · credits: {shown} · top up: {_BILLING}"


def test_just_above_a_dollar_shows_no_top_up_link(sl):
    _marker(sl, remaining_usd=1.01)
    assert strip_ansi(sl._credits_segment()) == " · credits: $1.01"


def test_threshold_is_one_dollar(sl):
    assert sl._CREDITS_LOW_USD == 1.0


def test_refusal_above_a_dollar_keeps_the_balance_and_names_the_operation(sl):
    _marker(sl, remaining_usd=2.04, payment_required=_refused("recall"))
    assert strip_ansi(sl._credits_segment()) == " · credits: $2.04 (not enough for recall)"


def test_refusal_above_a_dollar_shows_no_top_up_link(sl):
    _marker(sl, remaining_usd=2.04, payment_required=_refused("remember"))
    assert "top up" not in sl._credits_segment()


def test_refusal_at_a_low_balance_is_not_repeated_as_a_qualifier(sl):
    """Under a dollar the red number already says it; the qualifier would be noise."""
    _marker(sl, remaining_usd=0.04, payment_required=_refused("recall"))
    assert strip_ansi(sl._credits_segment()) == f" · credits: $0.04 · top up: {_BILLING}"


def test_refusal_without_a_balance_is_the_whole_segment(sl):
    """A dev tenant cannot fetch its balance (the platform host 401s), so the
    402 is the only credits signal it ever gets — it must still render."""
    _marker(sl, {"url:" + _CLOUD_URL: {"base_url": _CLOUD_URL, "payment_required": _refused()}})
    expected = f" · credits: not enough for recall · top up: {_BILLING}"
    assert strip_ansi(sl._credits_segment()) == expected


def test_refusal_survives_a_stale_balance_reading(sl):
    """Past the prune horizon the number is gone, but a fresh refusal still shows."""
    _marker(
        sl,
        remaining_usd=3.0,
        checked_at=time.time() - 8 * 24 * 3600,
        payment_required=_refused("save"),
    )
    expected = f" · credits: not enough for save · top up: {_BILLING}"
    assert strip_ansi(sl._credits_segment()) == expected


def test_stale_refusal_is_ignored(sl):
    _marker(sl, remaining_usd=2.04, payment_required=_refused("recall", age=8 * 24 * 3600))
    assert strip_ansi(sl._credits_segment()) == " · credits: $2.04"


def test_malformed_refusal_is_ignored(sl):
    _marker(sl, remaining_usd=2.04, payment_required={"at": time.time()})
    assert strip_ansi(sl._credits_segment()) == " · credits: $2.04"
    _marker(sl, remaining_usd=2.04, payment_required="recall")
    assert strip_ansi(sl._credits_segment()) == " · credits: $2.04"


def test_top_up_link_comes_last(sl):
    """After the cost and the age hint: the link is the way out, read last."""
    _marker(
        sl,
        remaining_usd=0.61,
        checked_at=time.time() - 20 * 60,
        last_op={"label": "turn", "cost_usd": 0.02, "at": time.time()},
    )
    expected = f" · credits: $0.61 · last turn ~$0.02 (20m ago) · top up: {_BILLING}"
    assert strip_ansi(sl._credits_segment()) == expected


def test_billing_url_env_override(sl, monkeypatch):
    monkeypatch.setenv("COGNEE_BILLING_URL", "https://billing.example.test/")
    _marker(sl, remaining_usd=0.5)
    assert strip_ansi(sl._credits_segment()).endswith(" · top up: https://billing.example.test/")


def test_billing_url_is_the_production_page_for_every_tenant(sl, monkeypatch):
    """The frontend host is not derivable from the tenant host (aws -> platform,
    dev-aws -> staging is a lookup, not a rule), so it stays a constant and
    staging/dev sessions override it."""
    dev = f"https://tenant-{_TENANT_ID}.dev-aws.cognee.ai"
    monkeypatch.setenv("COGNEE_BASE_URL", dev)
    _marker(sl, {_TENANT_ID: _entry(remaining_usd=0.5, base_url=dev)})
    assert strip_ansi(sl._credits_segment()) == f" · credits: $0.50 · top up: {_BILLING}"
    monkeypatch.setenv("COGNEE_BILLING_URL", "https://staging.cognee.ai/billing")
    expected = " · credits: $0.50 · top up: https://staging.cognee.ai/billing"
    assert strip_ansi(sl._credits_segment()) == expected


def test_low_balance_is_red_and_the_link_is_green(styled):
    _marker(styled, remaining_usd=0.61)
    expected = f" · {_RED}credits: $0.61{_RESET} · {_GREEN}top up: {_BILLING}{_RESET}"
    assert styled._credits_segment() == expected


def test_a_dollar_exactly_is_red(styled):
    _marker(styled, remaining_usd=1.0)
    assert styled._credits_segment().startswith(f" · {_RED}credits: $1.00{_RESET}")


def test_refusal_above_a_dollar_colours_only_the_qualifier(styled):
    """The number is still a healthy balance (green); the refusal is what is red."""
    _marker(styled, remaining_usd=2.04, payment_required=_refused("recall"))
    expected = f" · {_GREEN}credits: $2.04{_RESET} {_RED}(not enough for recall){_RESET}"
    assert styled._credits_segment() == expected


def test_refusal_without_a_balance_is_red_with_a_green_link(styled):
    _marker(styled, {"url:" + _CLOUD_URL: {"base_url": _CLOUD_URL, "payment_required": _refused()}})
    expected = (
        f" · {_RED}credits: not enough for recall{_RESET} · {_GREEN}top up: {_BILLING}{_RESET}"
    )
    assert styled._credits_segment() == expected


def test_low_segment_has_no_ansi_escapes(plain):
    _marker(plain, remaining_usd=0.61, payment_required=_refused())
    assert "\033" not in plain._credits_segment()
    _marker(plain, remaining_usd=2.04, payment_required=_refused())
    assert "\033" not in plain._credits_segment()
