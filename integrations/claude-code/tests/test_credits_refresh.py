"""Unit tests for _plugin_common.refresh_credits / read_credits_marker (SDK-355).

The credits marker feeds the status line's balance display. It is a MAP keyed
by tenant id — several concurrent Claude sessions on one machine can be
connected to different cloud tenants, and each needs its own balance entry.
The writer's contract: fetch the platform billing overview, select OUR
tenant's budget record (by tenant id → sole tenant → account aggregate),
update only our entry (atomically, under the credits lock), attribute the
spend delta since our tenant's previous reading to the operation that
triggered the refresh, and NEVER raise or clobber a good marker on failure.
Local (loopback) servers have no credits concept — the fetch must no-op.

Run: python integrations/claude-code/tests/test_credits_refresh.py (or via pytest).
"""

import json
import os
import pathlib
import sys
import tempfile

os.environ.setdefault("COGNEE_PLUGIN_IN_VENV", "1")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402

# The service URL cloud sessions actually configure is the per-tenant data
# plane; the billing routes live on the separate platform API host.
_TENANT_A = "f8c21da4-6674-4cc5-bc56-de5e93db881d"
_TENANT_B = "0b54dcbd-6b52-4b3e-a1dd-9d251e0f31bb"
_URL_A = f"https://tenant-{_TENANT_A}.aws.cognee.ai"
_URL_B = f"https://tenant-{_TENANT_B}.aws.cognee.ai"
_PLATFORM_URL = "https://api.aws.cognee.ai"


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


def _drive(fn, *, base_url=_URL_A, responses=None):
    """Run fn(calls) with the marker in a tmp dir and the HTTP layer stubbed.

    ``responses`` is a list consumed one per fetch; an Exception instance is
    raised instead of returned. State restored afterwards so the suite stays
    order-independent under pytest.
    """
    saved = {
        k: getattr(pc, k)
        for k in (
            "_CREDITS_MARKER",
            "_CREDITS_LOCK",
            "_json_http_request",
            "_local_api_url",
            "hook_log",
        )
    }
    saved_env = os.environ.get("COGNEE_PLATFORM_API_URL")
    os.environ.pop("COGNEE_PLATFORM_API_URL", None)
    queue = list(responses or [])
    calls = {"fetches": 0, "events": [], "base_urls": []}
    with tempfile.TemporaryDirectory() as tmp:
        pc._CREDITS_MARKER = pathlib.Path(tmp) / "credits.json"
        pc._CREDITS_LOCK = pathlib.Path(tmp) / "credits.lock"
        pc.hook_log = lambda ev, detail=None: calls["events"].append((ev, detail or {}))
        pc._local_api_url = lambda: base_url

        def _fake_request(path, payload=None, *, method="POST", timeout=30.0, base_url=None):
            assert path == "/api/v1/billing/credits/overview"
            assert method == "GET"
            calls["fetches"] += 1
            calls["base_urls"].append(base_url)
            item = queue.pop(0) if queue else {}
            if isinstance(item, Exception):
                raise item
            return item

        pc._json_http_request = _fake_request
        try:
            return fn(calls)
        finally:
            for k, v in saved.items():
                setattr(pc, k, v)
            if saved_env is None:
                os.environ.pop("COGNEE_PLATFORM_API_URL", None)
            else:
                os.environ["COGNEE_PLATFORM_API_URL"] = saved_env


# ── tenant selection ─────────────────────────────────────────────────────────


def test_tenant_id_selects_matching_tenant_entry():
    def _t(calls):
        entry = pc.refresh_credits(tenant_id=_TENANT_A)
        assert entry["remaining_usd"] == 9.5
        assert entry["spent_usd"] == 0.5
        assert entry["total_usd"] == 10.0
        assert entry["source"] == "tenant"
        assert entry["tenant_id"] == _TENANT_A
        on_disk = pc.read_credits_marker()
        assert set(on_disk) == {_TENANT_A}
        assert on_disk[_TENANT_A]["base_url"] == _URL_A

    _drive(
        _t,
        responses=[
            _overview(
                100.0,
                55.0,
                tenants=[
                    _tenant(_TENANT_B, 90.5, 54.5, 145.0),
                    _tenant(_TENANT_A, 9.5, 0.5, 10.0),
                ],
            )
        ],
    )


def test_single_tenant_fallback_learns_id():
    """No tenant id in hand + exactly one tenant in the account: use it and
    adopt its id as the map key."""

    def _t(calls):
        entry = pc.refresh_credits()
        assert entry["source"] == "single_tenant"
        assert entry["tenant_id"] == _TENANT_A
        assert set(pc.read_credits_marker()) == {_TENANT_A}

    _drive(_t, responses=[_overview(9.5, 0.5, tenants=[_tenant(_TENANT_A, 9.5, 0.5)])])


def test_account_budget_fallback_when_tenant_unknown():
    """Several tenants and no id: fall back to the account aggregate under the
    'account' key rather than guessing a tenant."""

    def _t(calls):
        entry = pc.refresh_credits()
        assert entry["source"] == "account"
        assert entry["remaining_usd"] == 100.0
        assert set(pc.read_credits_marker()) == {"account"}

    _drive(
        _t,
        responses=[
            _overview(
                100.0,
                55.0,
                tenants=[_tenant(_TENANT_A, 9.5, 0.5), _tenant(_TENANT_B, 90.5, 54.5)],
            )
        ],
    )


def test_tenantless_caller_recovers_tenant_from_url_binding():
    """The Stop-hook refresh has no tenant id; it must find the entry the
    prompt-time refresh bound to this base_url and keep keying it there."""

    def _t(calls):
        pc.refresh_credits(tenant_id=_TENANT_A)  # binds URL_A -> tenant A
        entry = pc.refresh_credits("turn")  # no tenant_id passed
        assert entry["tenant_id"] == _TENANT_A
        assert entry["source"] == "tenant"
        assert entry["last_op"]["label"] == "turn"
        assert abs(entry["last_op"]["cost_usd"] - 0.25) < 1e-9
        assert set(pc.read_credits_marker()) == {_TENANT_A}

    _drive(
        _t,
        responses=[
            _overview(100.0, 55.0, tenants=[_tenant(_TENANT_A, 9.5, 0.5)]),
            _overview(100.0, 55.0, tenants=[_tenant(_TENANT_A, 9.25, 0.75)]),
        ],
    )


# ── multi-tenant isolation ───────────────────────────────────────────────────


def test_second_tenant_gets_own_entry_and_first_survives():
    def _t(calls):
        pc.refresh_credits(tenant_id=_TENANT_A)

        pc._local_api_url = lambda: _URL_B  # terminal on the other tenant
        pc.refresh_credits(tenant_id=_TENANT_B)

        on_disk = pc.read_credits_marker()
        assert set(on_disk) == {_TENANT_A, _TENANT_B}
        assert on_disk[_TENANT_A]["base_url"] == _URL_A
        assert on_disk[_TENANT_B]["base_url"] == _URL_B

    _drive(
        _t,
        responses=[
            _overview(100.0, 55.0, tenants=[_tenant(_TENANT_A, 9.5, 0.5)]),
            _overview(100.0, 55.0, tenants=[_tenant(_TENANT_B, 90.5, 54.5)]),
        ],
    )


def test_other_tenants_spend_does_not_move_our_last_op():
    """Tenant B burning credits between our readings must not be attributed
    to tenant A's turn — the delta comes from OUR tenant's counter."""

    def _t(calls):
        pc.refresh_credits(tenant_id=_TENANT_A)
        entry = pc.refresh_credits("turn", tenant_id=_TENANT_A)
        # Tenant A's spend unchanged: no last_op despite tenant B's burn.
        assert "last_op" not in entry

    _drive(
        _t,
        responses=[
            _overview(
                100.0, 55.0, tenants=[_tenant(_TENANT_A, 9.5, 0.5), _tenant(_TENANT_B, 90.5, 54.5)]
            ),
            _overview(
                80.0, 75.0, tenants=[_tenant(_TENANT_A, 9.5, 0.5), _tenant(_TENANT_B, 70.5, 74.5)]
            ),
        ],
    )


# ── behavior carried over from the flat marker ───────────────────────────────


def test_local_server_noops():
    def _t(calls):
        assert pc.refresh_credits() == {}
        assert calls["fetches"] == 0
        assert not pc._CREDITS_MARKER.exists()

    _drive(_t, base_url="http://localhost:8011")


def test_fetch_targets_platform_api():
    def _t(calls):
        pc.refresh_credits(tenant_id=_TENANT_A)
        assert calls["base_urls"] == [_PLATFORM_URL]

    _drive(_t, responses=[_overview(9.5, 0.5, tenants=[_tenant(_TENANT_A, 9.5, 0.5)])])


def test_platform_url_env_override():
    def _t(calls):
        os.environ["COGNEE_PLATFORM_API_URL"] = "https://platform.example/"
        pc.refresh_credits()
        assert calls["base_urls"] == ["https://platform.example"]

    _drive(_t, responses=[_overview(14.23, 5.77)])


def test_delta_falls_back_to_remaining_drop():
    def _t(calls):
        pc.refresh_credits(tenant_id=_TENANT_A)
        entry = pc.refresh_credits("turn", tenant_id=_TENANT_A)
        assert abs(entry["last_op"]["cost_usd"] - 0.05) < 1e-9

    _drive(
        _t,
        responses=[
            _overview(0, 0, tenants=[_tenant(_TENANT_A, 14.23, None)]),
            _overview(0, 0, tenants=[_tenant(_TENANT_A, 14.18, None)]),
        ],
    )


def test_non_positive_delta_keeps_prior_last_op():
    def _t(calls):
        pc.refresh_credits(tenant_id=_TENANT_A)
        pc.refresh_credits("improve", tenant_id=_TENANT_A)  # arms last_op
        entry = pc.refresh_credits("turn", tenant_id=_TENANT_A)  # top-up: balance UP
        assert entry["last_op"]["label"] == "improve"
        assert entry["remaining_usd"] == 24.09

    _drive(
        _t,
        responses=[
            _overview(0, 0, tenants=[_tenant(_TENANT_A, 14.23, 5.77)]),
            _overview(0, 0, tenants=[_tenant(_TENANT_A, 14.09, 5.91)]),
            _overview(0, 0, tenants=[_tenant(_TENANT_A, 24.09, 5.91)]),
        ],
    )


def test_fetch_failure_leaves_marker_untouched():
    def _t(calls):
        pc.refresh_credits(tenant_id=_TENANT_A)
        assert pc.read_credits_marker()[_TENANT_A]["remaining_usd"] == 9.5
        assert pc.refresh_credits("turn") == {}
        assert pc.read_credits_marker()[_TENANT_A]["remaining_usd"] == 9.5
        assert any(ev == "credits_fetch_failed" for ev, _ in calls["events"])

    _drive(
        _t,
        responses=[
            _overview(0, 0, tenants=[_tenant(_TENANT_A, 9.5, 0.5)]),
            OSError("connection reset"),
        ],
    )


def test_empty_budget_payload_writes_nothing():
    def _t(calls):
        assert pc.refresh_credits() == {}
        assert not pc._CREDITS_MARKER.exists()
        assert any(ev == "credits_fetch_empty" for ev, _ in calls["events"])

    _drive(_t, responses=[{"budget": {}, "tenants": []}])


def test_read_marker_never_raises():
    def _t(calls):
        pc._CREDITS_MARKER.write_text("not json{{{", encoding="utf-8")
        assert pc.read_credits_marker() == {}

    _drive(_t)


# ── housekeeping ─────────────────────────────────────────────────────────────


def test_old_flat_format_is_migrated_away():
    """A pre-map flat marker (scalar top-level fields) is pruned on the first
    refresh rather than lingering as bogus map entries."""

    def _t(calls):
        pc._CREDITS_MARKER.write_text(
            json.dumps({"remaining_usd": 14.23, "base_url": _URL_A, "checked_at": 1.0}),
            encoding="utf-8",
        )
        pc.refresh_credits(tenant_id=_TENANT_A)
        assert set(pc.read_credits_marker()) == {_TENANT_A}

    _drive(_t, responses=[_overview(0, 0, tenants=[_tenant(_TENANT_A, 9.5, 0.5)])])


def test_ancient_entries_pruned():
    def _t(calls):
        stale = {"remaining_usd": 1.0, "base_url": _URL_B, "checked_at": 1.0}
        pc._CREDITS_MARKER.write_text(json.dumps({_TENANT_B: stale}), encoding="utf-8")
        pc.refresh_credits(tenant_id=_TENANT_A)
        assert set(pc.read_credits_marker()) == {_TENANT_A}

    _drive(_t, responses=[_overview(0, 0, tenants=[_tenant(_TENANT_A, 9.5, 0.5)])])


def test_lock_leftover_does_not_wedge_writer():
    """A stale credits.lock (crashed writer) is reclaimed; the refresh still
    lands."""

    def _t(calls):
        pc._CREDITS_LOCK.parent.mkdir(parents=True, exist_ok=True)
        pc._CREDITS_LOCK.write_text("", encoding="utf-8")
        os.utime(pc._CREDITS_LOCK, (1.0, 1.0))  # ancient mtime -> stale
        entry = pc.refresh_credits(tenant_id=_TENANT_A)
        assert entry["remaining_usd"] == 9.5

    _drive(_t, responses=[_overview(0, 0, tenants=[_tenant(_TENANT_A, 9.5, 0.5)])])


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
