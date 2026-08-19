"""Integration tests for _plugin_common.refresh_credits / read_credits_marker (SDK-355).

The credits marker feeds the status line's balance display. It is a MAP keyed
by tenant id — several concurrent agent sessions on one machine can be
connected to different cloud tenants, and each needs its own balance entry.
The writer's contract: fetch the platform billing overview, select OUR
tenant's budget record (exact tenant-id match or nothing), update only our
entry (atomically, under the credits lock), attribute the spend delta since
our tenant's previous reading to the operation that triggered the refresh, and
NEVER raise or clobber a good marker on failure. Local (loopback) servers have
no credits concept — the fetch must no-op.

Promoted to integration: the billing overview is served by a second mock acting
as the **platform API host**, which is the part worth proving — the memory data
plane (a per-tenant host) has no billing routes, so a request sent there 404s.
The marker and lock are real files under the per-test HOME.

Migrated from {claude-code,codex}/tests/test_credits_refresh.py.
"""

from __future__ import annotations

import json
import os

import pytest

OVERVIEW = "/api/v1/billing/credits/overview"

_TENANT_A = "f8c21da4-6674-4cc5-bc56-de5e93db881d"
_TENANT_B = "0b54dcbd-6b52-4b3e-a1dd-9d251e0f31bb"
# The URL a cloud session configures is the per-tenant data plane. It is never
# contacted by refresh_credits (only the platform host is), so a non-routable
# hostname here doubles as proof the billing call did not go to the data plane.
_URL_A = f"https://tenant-{_TENANT_A}.aws.cognee.ai"
_URL_B = f"https://tenant-{_TENANT_B}.aws.cognee.ai"


def _tenant(tenant_id, remaining, spent, max_budget=20.0):
    return {
        "tenantId": tenant_id,
        "tenantName": "ws",
        "remainingUsd": remaining,
        "spentUsd": spent,
        "maxBudgetUsd": max_budget,
    }


def _overview(remaining, spent, total=20.0, tenants=None):
    return {
        "budget": {"remainingUsd": remaining, "spentUsd": spent, "totalUsd": total},
        "tenants": tenants or [],
    }


@pytest.fixture
def pc(suite, isolated_modules, platform_server, monkeypatch):
    """_plugin_common wired to a cloud data-plane URL + the mock platform host."""
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setenv("COGNEE_BASE_URL", _URL_A)
    monkeypatch.setenv("COGNEE_PLATFORM_API_URL", platform_server.url)
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


@pytest.fixture
def events(pc, monkeypatch):
    """Capture hook_log events (the observable trace of each skip reason)."""
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(pc, "hook_log", lambda ev, detail=None: recorded.append((ev, detail or {})))
    return recorded


def _names(events) -> list[str]:
    return [ev for ev, _ in events]


# ── tenant selection ───────────────────────────────────────────────────────


def test_tenant_id_selects_matching_tenant_entry(pc, platform_server):
    platform_server.set_credits_overview(
        _overview(
            100.0,
            55.0,
            tenants=[_tenant(_TENANT_B, 90.5, 54.5, 145.0), _tenant(_TENANT_A, 9.5, 0.5, 10.0)],
        )
    )
    entry = pc.refresh_credits(tenant_id=_TENANT_A)
    assert entry["remaining_usd"] == 9.5
    assert entry["spent_usd"] == 0.5
    assert entry["total_usd"] == 10.0
    assert entry["tenant_id"] == _TENANT_A

    on_disk = pc.read_credits_marker()
    assert set(on_disk) == {_TENANT_A}
    assert on_disk[_TENANT_A]["base_url"] == _URL_A
    # The billing call went to the platform host, with the principal key.
    platform_server.assert_called("GET", OVERVIEW)


def test_tenantless_caller_with_no_binding_writes_nothing(pc, platform_server, events):
    """Connected tenant unknown (no id, no prior URL binding): strictly nothing
    is shown — not even a sole listed tenant — and the doomed fetch is skipped."""
    platform_server.set_credits_overview(
        _overview(9.5, 0.5, tenants=[_tenant(_TENANT_A, 9.5, 0.5)])
    )
    assert pc.refresh_credits() == {}
    platform_server.assert_not_called("GET", OVERVIEW)
    assert not pc._CREDITS_MARKER.exists()
    assert "credits_refresh_skipped_no_tenant" in _names(events)


def test_explicit_tenant_not_in_overview_writes_nothing(pc, platform_server, events):
    """An explicit tenant id absent from the overview (e.g. connected to a
    shared tenant someone else owns): nothing is written or shown — never
    another workspace's budget, never the all-tenants aggregate."""
    platform_server.set_credits_overview(
        _overview(100.0, 55.0, tenants=[_tenant(_TENANT_B, 90.5, 54.5)])
    )
    assert pc.refresh_credits(tenant_id=_TENANT_A) == {}
    platform_server.assert_called("GET", OVERVIEW)
    assert not pc._CREDITS_MARKER.exists()
    assert "credits_tenant_not_in_overview" in _names(events)


def test_explicit_tenant_miss_never_adopts_the_sole_listed_tenant(pc, platform_server):
    """Blocker regression (PR review): an explicit tenant id that is NOT in the
    overview's tenants list must not adopt the single listed tenant (someone's
    other workspace) — under exact-match-or-nothing it writes nothing at all."""
    platform_server.set_credits_overview(
        _overview(100.0, 55.0, tenants=[_tenant(_TENANT_B, 90.5, 54.5)])
    )
    assert pc.refresh_credits(tenant_id=_TENANT_A) == {}
    assert _TENANT_B not in pc.read_credits_marker()


def test_tenantless_caller_recovers_tenant_from_url_binding(pc, platform_server):
    """The Stop-hook refresh has no tenant id; it must find the entry the
    prompt-time refresh bound to this base_url and keep keying it there."""
    platform_server.set_credits_overview(
        [
            _overview(100.0, 55.0, tenants=[_tenant(_TENANT_A, 9.5, 0.5)]),
            _overview(100.0, 55.0, tenants=[_tenant(_TENANT_A, 9.25, 0.75)]),
        ]
    )
    pc.refresh_credits(tenant_id=_TENANT_A)  # binds URL_A -> tenant A
    entry = pc.refresh_credits("turn")  # no tenant_id passed
    assert entry["tenant_id"] == _TENANT_A
    assert entry["last_op"]["label"] == "turn"
    assert abs(entry["last_op"]["cost_usd"] - 0.25) < 1e-9
    assert set(pc.read_credits_marker()) == {_TENANT_A}


# ── multi-tenant isolation ─────────────────────────────────────────────────


def test_second_tenant_gets_own_entry_and_first_survives(pc, platform_server, monkeypatch):
    platform_server.set_credits_overview(
        [
            _overview(100.0, 55.0, tenants=[_tenant(_TENANT_A, 9.5, 0.5)]),
            _overview(100.0, 55.0, tenants=[_tenant(_TENANT_B, 90.5, 54.5)]),
        ]
    )
    pc.refresh_credits(tenant_id=_TENANT_A)

    monkeypatch.setenv("COGNEE_BASE_URL", _URL_B)  # terminal on the other tenant
    pc.refresh_credits(tenant_id=_TENANT_B)

    on_disk = pc.read_credits_marker()
    assert set(on_disk) == {_TENANT_A, _TENANT_B}
    assert on_disk[_TENANT_A]["base_url"] == _URL_A
    assert on_disk[_TENANT_B]["base_url"] == _URL_B


def test_other_tenants_spend_does_not_move_our_last_op(pc, platform_server):
    """Tenant B burning credits between our readings must not be attributed to
    tenant A's turn — the delta comes from OUR tenant's counter."""
    platform_server.set_credits_overview(
        [
            _overview(
                100.0, 55.0, tenants=[_tenant(_TENANT_A, 9.5, 0.5), _tenant(_TENANT_B, 90.5, 54.5)]
            ),
            _overview(
                80.0, 75.0, tenants=[_tenant(_TENANT_A, 9.5, 0.5), _tenant(_TENANT_B, 70.5, 74.5)]
            ),
        ]
    )
    pc.refresh_credits(tenant_id=_TENANT_A)
    entry = pc.refresh_credits("turn", tenant_id=_TENANT_A)
    # Tenant A's spend unchanged: no last_op despite tenant B's burn.
    assert "last_op" not in entry


# ── gating, spend attribution, failure ─────────────────────────────────────


def test_local_server_noops(pc, platform_server, monkeypatch):
    """A loopback data plane has no credits concept: skipped before any fetch."""
    monkeypatch.setenv("COGNEE_BASE_URL", "http://localhost:8011")
    assert pc.refresh_credits() == {}
    platform_server.assert_not_called("GET", OVERVIEW)
    assert not pc._CREDITS_MARKER.exists()


def test_platform_url_env_override_is_honoured(pc, platform_server):
    """The override is what routes billing away from the data plane."""
    platform_server.set_credits_overview(_overview(0, 0, tenants=[_tenant(_TENANT_A, 9.5, 0.5)]))
    entry = pc.refresh_credits(tenant_id=_TENANT_A)
    assert entry["remaining_usd"] == 9.5
    assert len(platform_server.calls) == 1


def test_delta_falls_back_to_remaining_drop(pc, platform_server):
    platform_server.set_credits_overview(
        [
            _overview(0, 0, tenants=[_tenant(_TENANT_A, 14.23, None)]),
            _overview(0, 0, tenants=[_tenant(_TENANT_A, 14.18, None)]),
        ]
    )
    pc.refresh_credits(tenant_id=_TENANT_A)
    entry = pc.refresh_credits("turn", tenant_id=_TENANT_A)
    assert abs(entry["last_op"]["cost_usd"] - 0.05) < 1e-9


def test_non_positive_delta_keeps_prior_last_op(pc, platform_server):
    platform_server.set_credits_overview(
        [
            _overview(0, 0, tenants=[_tenant(_TENANT_A, 14.23, 5.77)]),
            _overview(0, 0, tenants=[_tenant(_TENANT_A, 14.09, 5.91)]),
            _overview(0, 0, tenants=[_tenant(_TENANT_A, 24.09, 5.91)]),
        ]
    )
    pc.refresh_credits(tenant_id=_TENANT_A)
    pc.refresh_credits("improve", tenant_id=_TENANT_A)  # arms last_op
    entry = pc.refresh_credits("turn", tenant_id=_TENANT_A)  # top-up: balance UP
    assert entry["last_op"]["label"] == "improve"
    assert entry["remaining_usd"] == 24.09


def test_fetch_failure_leaves_marker_untouched(pc, platform_server, events):
    platform_server.set_credits_overview(
        [_overview(0, 0, tenants=[_tenant(_TENANT_A, 9.5, 0.5)]), 500]
    )
    pc.refresh_credits(tenant_id=_TENANT_A)
    assert pc.read_credits_marker()[_TENANT_A]["remaining_usd"] == 9.5
    assert pc.refresh_credits("turn") == {}
    assert pc.read_credits_marker()[_TENANT_A]["remaining_usd"] == 9.5
    assert "credits_fetch_failed" in _names(events)


def test_empty_budget_payload_writes_nothing(pc, platform_server, events):
    # The connected tenant exists in the overview but reports null values.
    platform_server.set_credits_overview(_overview(0, 0, tenants=[_tenant(_TENANT_A, None, None)]))
    assert pc.refresh_credits(tenant_id=_TENANT_A) == {}
    assert not pc._CREDITS_MARKER.exists()
    assert "credits_fetch_empty" in _names(events)


# ── housekeeping ───────────────────────────────────────────────────────────


def test_read_marker_never_raises(pc):
    pc._CREDITS_MARKER.parent.mkdir(parents=True, exist_ok=True)
    pc._CREDITS_MARKER.write_text("not json{{{", encoding="utf-8")
    assert pc.read_credits_marker() == {}


def test_old_flat_format_is_migrated_away(pc, platform_server):
    """A pre-map flat marker (scalar top-level fields) is pruned on the first
    refresh rather than lingering as bogus map entries."""
    platform_server.set_credits_overview(_overview(0, 0, tenants=[_tenant(_TENANT_A, 9.5, 0.5)]))
    pc._CREDITS_MARKER.parent.mkdir(parents=True, exist_ok=True)
    pc._CREDITS_MARKER.write_text(
        json.dumps({"remaining_usd": 14.23, "base_url": _URL_A, "checked_at": 1.0}),
        encoding="utf-8",
    )
    pc.refresh_credits(tenant_id=_TENANT_A)
    assert set(pc.read_credits_marker()) == {_TENANT_A}


def test_ancient_entries_pruned(pc, platform_server):
    platform_server.set_credits_overview(_overview(0, 0, tenants=[_tenant(_TENANT_A, 9.5, 0.5)]))
    pc._CREDITS_MARKER.parent.mkdir(parents=True, exist_ok=True)
    stale = {"remaining_usd": 1.0, "base_url": _URL_B, "checked_at": 1.0}
    pc._CREDITS_MARKER.write_text(json.dumps({_TENANT_B: stale}), encoding="utf-8")
    pc.refresh_credits(tenant_id=_TENANT_A)
    assert set(pc.read_credits_marker()) == {_TENANT_A}


def test_lock_leftover_does_not_wedge_writer(pc, platform_server):
    """A stale credits.lock (crashed writer) is reclaimed; the refresh still lands."""
    platform_server.set_credits_overview(_overview(0, 0, tenants=[_tenant(_TENANT_A, 9.5, 0.5)]))
    pc._CREDITS_LOCK.parent.mkdir(parents=True, exist_ok=True)
    pc._CREDITS_LOCK.write_text("", encoding="utf-8")
    os.utime(pc._CREDITS_LOCK, (1.0, 1.0))  # ancient mtime -> stale
    entry = pc.refresh_credits(tenant_id=_TENANT_A)
    assert entry["remaining_usd"] == 9.5
