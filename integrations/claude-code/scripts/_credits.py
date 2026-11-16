"""The credits marker and the cloud billing reading, split out of
_plugin_common.py to keep that file under the plugin directory's per-file size
limit.

Import it through _plugin_common, which re-exports every name here. Names that
live in _plugin_common are looked up there at call time (``_pc.<name>``), and so
are the helpers tests patch on _plugin_common, so a monkeypatch there reaches
this module too.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timezone

import _plugin_common as _pc

# --- Credits marker (status-line budget display, SDK-355) ---------------------
# The status-line renderer is pure-local by contract, so the credits balance it
# shows comes from this marker, written by hooks/watchers that are already
# allowed to touch the network. Cloud-only: a local server has no credit
# concept, so the fetch is gated on a non-loopback base URL (the renderer
# independently gates on its mode label).
_CREDITS_MARKER = _pc._PLUGIN_DIR / "credits.json"
_PLATFORM_API_URL_DEFAULT = "https://api.aws.cognee.ai"


def _platform_host_for(service_url: str) -> str:
    """``api.<env>`` for a ``tenant-<id>.<env>`` data-plane host, else "".

    The cloud names its hosts by role under one environment suffix: the memory
    data plane is ``tenant-<id>.<env>`` and the platform (billing, account) is
    ``api.<env>``. Deriving one from the other keeps a dev tenant talking to
    the dev platform — asking production for a dev tenant's balance 401s, and
    the status line then never showed a number at all.
    """
    host = (urllib.parse.urlparse(str(service_url or "").strip()).hostname or "").lower()
    label, _, rest = host.partition(".")
    if not rest or not label.startswith("tenant-") or len(label) <= len("tenant-"):
        return ""
    return f"api.{rest}"


def _platform_api_url() -> str:
    """The cloud control-plane API host (billing/account routes).

    Distinct from the memory data plane: cloud sessions talk to a per-tenant
    host (``tenant-<id>.aws.cognee.ai``), which serves recall/remember/improve
    but has NO billing routes — asking it for the credits overview 404s. The
    billing routes live only on the platform API, which accepts the same
    tenant ``COGNEE_API_KEY``. Derived from the configured service URL
    (``api.<env>`` beside ``tenant-<id>.<env>``); ``COGNEE_PLATFORM_API_URL``
    overrides, and a non-tenant URL falls back to the production platform.
    """
    override = str(os.environ.get("COGNEE_PLATFORM_API_URL", "") or "").strip()
    if override:
        return override.rstrip("/")
    host = _platform_host_for(_pc._local_api_url())
    return f"https://{host}" if host else _PLATFORM_API_URL_DEFAULT


# The marker is a MAP keyed by tenant id: several concurrent Claude sessions
# on one machine can be connected to DIFFERENT cloud tenants, and a flat
# last-writer-wins record made them clobber each other's balance. Each entry
# carries the service base_url it was observed under — that binding is how
# readers with only a URL in hand (the renderer, the Stop hook) find their
# tenant's entry.
_CREDITS_LOCK = _pc._PLUGIN_DIR / "credits.lock"
_CREDITS_LOCK_STALE_SECONDS = 30.0
_CREDITS_ENTRY_MAX_AGE_SECONDS = 7 * 24 * 3600.0


def read_credits_marker() -> dict:
    """Return the tenant-keyed credits map, or {} — never raises."""
    try:
        raw = json.loads(_CREDITS_MARKER.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _credits_entry_for_url(marker: dict, service_url: str) -> tuple[str, dict]:
    """Find the (tenant_id, entry) bound to ``service_url``, or ("", {})."""
    want = _pc._normalize_service_url(service_url)
    for key, entry in marker.items():
        if (
            isinstance(entry, dict)
            and _pc._normalize_service_url(str(entry.get("base_url") or "")) == want
        ):
            return str(key), entry
    return "", {}


# --- "Not enough credits" (HTTP 402) ------------------------------------------
# A 402 from a billable route (recall / remember / improve / entry save) is the
# server saying "this tenant cannot pay for THIS request" — the only positive
# exhaustion signal the plugin ever sees, and the only one that works when the
# billing overview itself cannot be fetched (a dev tenant asking the wrong
# platform host, an expired key). It is recorded on the tenant's marker entry
# next to the balance, and cleared by the next billable operation that
# succeeds. The status line renders it as "(not enough for <op>)" beside the
# balance, or as the whole segment when no balance reading exists.
_CREDITS_URL_KEY_PREFIX = "url:"


def _placeholder_credits_key(service_url: str) -> str:
    """Marker key for a 402 seen before any tenant-bound balance entry exists.

    The recall hook has no tenant id (``load_resolved(identity=False)``), and a
    dev tenant never gets a balance entry because its billing fetch fails, so
    the note needs a home keyed by the service URL. ``refresh_credits`` folds
    it into the real tenant entry the moment one is written.
    """
    return _CREDITS_URL_KEY_PREFIX + _pc._normalize_service_url(service_url)


def _write_credits_marker(marker: dict) -> None:
    """Atomically replace the credits marker. Caller holds the credits lock."""
    _CREDITS_MARKER.parent.mkdir(parents=True, exist_ok=True)
    # Per-pid tmp: a shared staging name let one writer truncate the file
    # another was about to os.replace into place, and the renderer briefly saw
    # a torn marker (the "credits disappear mid-search" flicker).
    tmp = _CREDITS_MARKER.with_name(f"{_CREDITS_MARKER.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(marker), encoding="utf-8")
        os.replace(tmp, _CREDITS_MARKER)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _credits_entry_last_seen(entry: dict) -> float:
    """The newest timestamp on an entry: balance reading or 402 note."""
    stamps = [entry.get("checked_at", 0)]
    note = entry.get("payment_required")
    if isinstance(note, dict):
        stamps.append(note.get("at", 0))
    best = 0.0
    for stamp in stamps:
        try:
            best = max(best, float(stamp or 0))
        except (TypeError, ValueError):
            continue
    return best


def _locate_credits_entry(marker: dict, service_url: str, tenant_id: str = "") -> str:
    """The marker key for this session's tenant: explicit id, URL binding, or
    the URL placeholder (which may not exist yet)."""
    key = str(tenant_id or "").strip()
    if key:
        return key
    key, _ = _credits_entry_for_url(marker, service_url)
    return key or _placeholder_credits_key(service_url)


def record_payment_required(op_label: str, *, tenant_id: str = "") -> None:
    """Note that ``op_label`` was refused with HTTP 402 on this tenant.

    Best-effort and never raises: the caller is a hook on the keystroke->answer
    path or a background sync, and a marker problem must not become theirs.
    No-op on a local server (no credits concept).
    """
    service_url = _pc._local_api_url()
    if _pc.service_url_is_local(service_url):
        return
    op = str(op_label or "operation").strip()[:24] or "operation"
    try:
        acquired = _pc._try_acquire_credits_lock()
        try:
            marker = read_credits_marker()
            key = _locate_credits_entry(marker, service_url, tenant_id)
            entry = marker.get(key)
            entry = dict(entry) if isinstance(entry, dict) else {}
            entry.setdefault("base_url", service_url)
            entry["payment_required"] = {
                "op": op,
                "at": datetime.now(timezone.utc).timestamp(),
            }
            marker[key] = entry
            _pc._write_credits_marker(marker)
        finally:
            if acquired:
                _release_credits_lock()
        _pc.hook_log("credits_payment_required", {"op": op, "base_url": service_url})
    except Exception as exc:
        _pc.hook_log("credits_marker_write_failed", {"error": str(exc)[:200], "op": op})


def clear_payment_required(*, tenant_id: str = "") -> None:
    """Drop the 402 note once a billable operation on this tenant succeeded.

    Cheap when there is nothing to clear (one small read, no lock, no write),
    which is the steady state on every prompt. Never raises; no-op locally.
    """
    service_url = _pc._local_api_url()
    if _pc.service_url_is_local(service_url):
        return
    try:
        marker = read_credits_marker()
        key = _locate_credits_entry(marker, service_url, tenant_id)
        entry = marker.get(key)
        if not isinstance(entry, dict) or "payment_required" not in entry:
            return
        acquired = _pc._try_acquire_credits_lock()
        try:
            marker = read_credits_marker()
            entry = marker.get(key)
            if not isinstance(entry, dict):
                return
            entry = dict(entry)
            note = entry.pop("payment_required", None)
            if key.startswith(_CREDITS_URL_KEY_PREFIX) and "remaining_usd" not in entry:
                # A placeholder held nothing but the note: remove it outright.
                marker.pop(key, None)
            else:
                marker[key] = entry
            _pc._write_credits_marker(marker)
        finally:
            if acquired:
                _release_credits_lock()
        _pc.hook_log(
            "credits_payment_cleared",
            {"op": (note or {}).get("op") if isinstance(note, dict) else None},
        )
    except Exception as exc:
        _pc.hook_log("credits_marker_write_failed", {"error": str(exc)[:200], "op": "clear"})


def _try_acquire_credits_lock() -> bool:
    """Guard the marker's read-modify-write; concurrent writers on different
    tenants would otherwise each write back a map missing the other's entry.
    Fail-open like the drain lock: a rare lost update beats a wedged marker."""
    try:
        _pc._PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        if _CREDITS_LOCK.exists():
            try:
                if time.time() - _CREDITS_LOCK.stat().st_mtime > _CREDITS_LOCK_STALE_SECONDS:
                    _CREDITS_LOCK.unlink()
            except FileNotFoundError:
                pass
        fd = os.open(str(_CREDITS_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return True


def _release_credits_lock() -> None:
    try:
        _CREDITS_LOCK.unlink()
    except Exception:
        pass


def _select_tenant_budget(overview: dict, tenant_id: str) -> dict | None:
    """Return the budget record of OUR tenant from the credits overview, or None.

    Exact match only, on purpose: the display answers "what does the tenant I
    am connected to have left?", and no other number is a valid answer. The
    overview's account-wide ``budget`` aggregates every workspace the user
    owns, and its ``tenants`` list may contain workspaces other than the one
    this session talks to (e.g. your personal workspace while connected to a
    shared tenant someone else owns) — showing either would be wrong, so with
    no exact match the caller shows nothing at all.
    """
    tenants = overview.get("tenants")
    tenants = [t for t in tenants if isinstance(t, dict)] if isinstance(tenants, list) else []
    for t in tenants:
        if str(t.get("tenantId") or "").strip() == tenant_id:
            return {
                "remaining_usd": t.get("remainingUsd"),
                "spent_usd": t.get("spentUsd"),
                "total_usd": t.get("maxBudgetUsd"),
            }
    return None


def refresh_credits(op_label: str = "", *, tenant_id: str = "", timeout: float = 3.0) -> dict:
    """Fetch the cloud credits overview and update this tenant's marker entry.

    Best-effort by contract: any failure returns {} and leaves the existing
    marker untouched — the renderer shows the age of an old reading instead,
    and a fetch problem must never propagate into the calling hook.

    ``tenant_id`` comes from ``load_resolved()`` (the connections/me lookup)
    when the caller has it; otherwise the tenant is recovered from the marker
    entry already bound to this service URL (established by the prompt-time
    refresh). Strictly the CONNECTED tenant's budget or nothing: when the
    tenant cannot be determined, or is not in the overview, no entry is
    written and the segment simply does not render.

    ``op_label`` ("turn" / "remember" / "improve") attributes the spend
    recorded since the previous reading OF THIS TENANT to the operation that
    just ran. Approximate by design — the cloud aggregates spend
    asynchronously and concurrent operations overlap, so the delta reads as
    "~cost", not an invoice. A non-positive delta (aggregation lag, or a
    top-up between readings) refreshes the balance but records no last_op:
    a negative "cost" is meaningless, and the prior last_op is kept so the
    display doesn't flicker away on every idle refresh.
    """
    service_url = _pc._local_api_url()
    if _pc.service_url_is_local(service_url):
        return {}
    platform_url = _platform_api_url()
    try:
        marker = read_credits_marker()
        tenant_id = str(tenant_id or "").strip()
        if not tenant_id:
            tenant_id, _ = _credits_entry_for_url(marker, service_url)
        if not tenant_id:
            # Connected tenant unknown (no id from the caller, no prior URL
            # binding): show nothing rather than someone's other workspace or
            # the all-tenants aggregate. Skipped BEFORE the fetch — a doomed
            # lookup is not worth a network call.
            _pc.hook_log("credits_refresh_skipped_no_tenant", {"base_url": service_url})
            return {}
        overview = _pc._json_http_request(
            "/api/v1/billing/credits/overview",
            None,
            method="GET",
            timeout=timeout,
            base_url=platform_url,
        )
        budget = _select_tenant_budget(overview or {}, tenant_id)
        if budget is None:
            _pc.hook_log(
                "credits_tenant_not_in_overview",
                {"tenant_id": tenant_id, "platform_url": platform_url},
            )
            return {}
        remaining = budget.get("remaining_usd")
        spent = budget.get("spent_usd")
        if remaining is None and spent is None:
            _pc.hook_log(
                "credits_fetch_empty",
                {"platform_url": platform_url, "tenant_id": tenant_id},
            )
            return {}
        now_ts = datetime.now(timezone.utc).timestamp()
        entry_key = tenant_id
        entry = {
            "remaining_usd": remaining,
            "spent_usd": spent,
            "total_usd": budget.get("total_usd"),
            # The service URL this tenant was observed under: the renderer and
            # tenantless callers look their entry up by it.
            "base_url": service_url,
            "platform_url": platform_url,
            "tenant_id": tenant_id,
            "checked_at": now_ts,
        }
        acquired = _pc._try_acquire_credits_lock()
        try:
            # Re-read under the lock: another tenant's refresh may have
            # updated the map since the pre-fetch read.
            marker = read_credits_marker()
            prior = marker.get(entry_key)
            prior = prior if isinstance(prior, dict) else {}
            last_op = prior.get("last_op")
            # A 402 note outlives the balance refresh: a small positive balance
            # can still be "not enough", and only a SUCCESSFUL operation knows
            # otherwise. Adopt a note parked on the URL placeholder too — the
            # recall hook records without a tenant id, and this is the first
            # tenant-bound entry for the URL.
            placeholder = marker.get(_placeholder_credits_key(service_url))
            payment_required = prior.get("payment_required")
            if not isinstance(payment_required, dict) and isinstance(placeholder, dict):
                payment_required = placeholder.get("payment_required")
            if op_label:
                delta = None
                try:
                    # Prefer the spend counter; fall back to the remaining-
                    # balance drop when the API reports only one of the two.
                    if spent is not None and prior.get("spent_usd") is not None:
                        delta = float(spent) - float(prior["spent_usd"])
                    elif remaining is not None and prior.get("remaining_usd") is not None:
                        delta = float(prior["remaining_usd"]) - float(remaining)
                except (TypeError, ValueError):
                    delta = None
                if delta is not None and delta > 0:
                    last_op = {
                        "label": str(op_label)[:24],
                        "cost_usd": round(delta, 4),
                        "at": now_ts,
                    }
            if isinstance(last_op, dict):
                entry["last_op"] = last_op
            if isinstance(payment_required, dict):
                entry["payment_required"] = payment_required
            marker[entry_key] = entry
            marker.pop(_placeholder_credits_key(service_url), None)
            # Prune long-dead tenants so one-off connections don't accumulate.
            for key in [
                k
                for k, v in marker.items()
                if k != entry_key
                and (
                    not isinstance(v, dict)
                    or now_ts - _credits_entry_last_seen(v) > _CREDITS_ENTRY_MAX_AGE_SECONDS
                )
            ]:
                marker.pop(key, None)
            _pc._write_credits_marker(marker)
        finally:
            if acquired:
                _release_credits_lock()
        return entry
    except Exception as exc:
        # platform_url in the detail: a 404 here once cost a PID-correlation
        # hunt to discover WHICH host was being asked.
        _pc.hook_log(
            "credits_fetch_failed",
            {"error": str(exc)[:200], "platform_url": platform_url},
        )
        return {}
