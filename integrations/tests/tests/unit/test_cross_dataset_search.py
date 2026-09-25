"""Cross-dataset search: "not in the active dataset? offer another one".

Recall is scoped to the launch's ACTIVE dataset. When its knowledge graph
answers with nothing, the prompt hook names the other datasets the user can
read and how to run a one-off graph search on one of them — no switch, no new
session. The skills drive the same flow from ``list-datasets.py``.

Contract:
  * ``list_readable_datasets`` is the read set exactly as ``GET /api/v1/datasets``
    returns it — no permission calls, read-only rows included
    (``list_writable_datasets`` builds on it; its own tests live in
    test_dataset_switch.py);
  * ``other_readable_datasets`` drops the active dataset by every handle it
    goes by (its UUIDs under shared memory, its name when name-addressed);
  * ``cached_readable_datasets`` returns the rows: a fresh cache without the
    network, a stale one refreshed, the stale rows kept when the refresh fails,
    and never a listing fetched for another server/identity;
  * the hook appends the hint on every prompt the server ANSWERED — hits or
    not, since graph retrieval always returns something and only the model
    can judge whether it answers the user — and never when nothing answered
    (dead server, open breaker), with a single dataset, or with the knob off;
    the hint never reaches the header;
  * ``list-datasets.py`` marks the active dataset and can drop it.

Both hook suites carry the flow identically.
"""

from __future__ import annotations

import json
import time
import urllib.error

import pytest
from utils.recall import HIT, SCOPES, URL, arm_code_lane, drive_recall, load_lookup


@pytest.fixture(autouse=True)
def _needs_cross_dataset_search(suite):
    if not suite.has_cross_dataset_search:
        pytest.skip(f"{suite.name}: no cross-dataset search flow")


ACTIVE_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"
THIRD_ID = "33333333-3333-4333-8333-333333333333"

ROWS = [
    {"name": "agent_sessions", "id": ACTIVE_ID, "owner_id": "me"},
    {"name": "project-alpha", "id": OTHER_ID, "owner_id": "me"},
    {"name": "team-notes", "id": THIRD_ID, "owner_id": "someone"},
]

_MISS = {scope: [] for scope in SCOPES}


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


def test_readable_is_the_read_set_with_no_permission_calls(pc, monkeypatch):
    paths: list[str] = []

    def response(path, *args, **kwargs):
        paths.append(path)
        return [
            {"name": "team-notes", "id": THIRD_ID, "ownerId": "someone"},
            {"name": "agent_sessions", "id": ACTIVE_ID, "owner_id": "me"},
            "junk",
        ]

    monkeypatch.setattr(pc, "_json_http_request", response)
    assert pc.list_readable_datasets() == [
        {"name": "agent_sessions", "id": ACTIVE_ID, "owner_id": "me"},
        {"name": "team-notes", "id": THIRD_ID, "owner_id": "someone"},
    ]
    assert paths == ["/api/v1/datasets/"]


def test_others_excludes_active_by_uuid_under_shared_memory(pc):
    others = pc.other_readable_datasets(ROWS, "agent_sessions", [ACTIVE_ID])
    assert [r["id"] for r in others] == [OTHER_ID, THIRD_ID]


def test_others_excludes_same_named_copies_graph_recall_already_spans(pc):
    rows = ROWS + [{"name": "agent_sessions", "id": "44444444-4444-4444-8444-444444444444"}]
    others = pc.other_readable_datasets(rows, ACTIVE_ID, [ACTIVE_ID, rows[-1]["id"]])
    assert [r["id"] for r in others] == [OTHER_ID, THIRD_ID]


def test_others_excludes_active_by_name_when_name_addressed(pc):
    """No ids on the launch record (separated memory / older server): the name
    is the only handle, so a same-named row is the active dataset."""
    others = pc.other_readable_datasets(ROWS, "agent_sessions", [])
    assert [r["name"] for r in others] == ["project-alpha", "team-notes"]


def test_others_dedupes_and_skips_junk(pc):
    rows = [ROWS[1], ROWS[1], "junk", {"name": "no-id"}]
    assert [r["id"] for r in pc.other_readable_datasets(rows, "x", [])] == [OTHER_ID]


# ── cache ──────────────────────────────────────────────────────────────────


@pytest.fixture
def cache(pc, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(pc, "_local_api_url", lambda: URL)
    monkeypatch.setattr(pc, "_api_key", lambda: "key-1")

    def listing(*a, **k):
        calls.append("server")
        return list(ROWS)

    monkeypatch.setattr(pc, "list_readable_datasets", listing)
    return calls


def test_first_call_fetches_and_writes_the_cache(pc, cache):
    assert pc.cached_readable_datasets(service_url=URL) == ROWS
    assert cache == ["server"]
    stored = json.loads(pc._READABLE_DATASETS_CACHE.read_text(encoding="utf-8"))
    assert stored["datasets"] == ROWS and stored["key"]


def test_fresh_cache_is_served_without_the_network(pc, cache):
    pc.cached_readable_datasets(service_url=URL)
    assert pc.cached_readable_datasets(service_url=URL) == ROWS
    assert cache == ["server"]


def test_stale_cache_is_refreshed(pc, cache, monkeypatch):
    pc.cached_readable_datasets(service_url=URL)
    stored = json.loads(pc._READABLE_DATASETS_CACHE.read_text(encoding="utf-8"))
    stored["fetched_at"] = time.time() - 10_000
    pc._READABLE_DATASETS_CACHE.write_text(json.dumps(stored), encoding="utf-8")
    assert pc.cached_readable_datasets(service_url=URL) == ROWS
    assert cache == ["server", "server"]


def test_stale_cache_without_refresh_serves_the_stale_rows(pc, cache):
    pc.cached_readable_datasets(service_url=URL)
    assert pc.cached_readable_datasets(service_url=URL, max_age=0, refresh=False) == ROWS
    assert cache == ["server"]


def test_failed_refresh_keeps_the_stale_listing(pc, cache, monkeypatch):
    pc.cached_readable_datasets(service_url=URL)

    def boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(pc, "list_readable_datasets", boom)
    assert pc.cached_readable_datasets(service_url=URL, max_age=0) == ROWS


def test_failed_refresh_with_no_cache_is_empty_not_an_error(pc, monkeypatch):
    monkeypatch.setattr(pc, "_local_api_url", lambda: URL)
    monkeypatch.setattr(pc, "_api_key", lambda: "key-1")

    def boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(pc, "list_readable_datasets", boom)
    assert pc.cached_readable_datasets(service_url=URL) == []


def test_cache_is_keyed_by_server_and_identity(pc, cache, monkeypatch):
    pc.cached_readable_datasets(service_url=URL)
    pc.cached_readable_datasets(service_url="https://elsewhere.example")
    monkeypatch.setattr(pc, "_api_key", lambda: "key-2")
    pc.cached_readable_datasets(service_url=URL)
    assert cache == ["server", "server", "server"]


# ── the hook's hint ────────────────────────────────────────────────────────


@pytest.fixture
def lookup(suite, hook_module, monkeypatch):
    module = load_lookup(suite, hook_module, monkeypatch)
    monkeypatch.setattr(
        module, "resolve_active_dataset_ids", lambda *a, **k: (ACTIVE_ID, [ACTIVE_ID])
    )
    monkeypatch.setattr(module, "get_dataset", lambda config: "agent_sessions")
    return module


def _offer(lookup, monkeypatch, rows=ROWS):
    seen: list[dict] = []

    def cached(**kw):
        seen.append(kw)
        return list(rows)

    monkeypatch.setattr(lookup, "cached_readable_datasets", cached)
    return seen


def _context(run) -> str:
    return run.output["hookSpecificOutput"]["additionalContext"]


def test_the_hint_names_the_hosts_way_of_asking(suite, lookup, monkeypatch):
    """Claude has an interactive picker; Codex has none outside plan mode, so
    each host's hook spells out its own way to put the choice to the user —
    a block that only says "offer them" gets answered in prose."""
    _offer(lookup, monkeypatch)
    ctx = _context(drive_recall(lookup, monkeypatch, recall=_MISS))
    if suite.name == "claude-code":
        assert "AskUserQuestion" in ctx and "numbered list" not in ctx
    else:
        assert "numbered list" in ctx and "AskUserQuestion" not in ctx


def test_an_answered_prompt_appends_the_other_datasets(lookup, monkeypatch):
    _offer(lookup, monkeypatch)
    run = drive_recall(lookup, monkeypatch, recall=_MISS)
    ctx = _context(run)
    assert "(no memory matches for this prompt)" in ctx
    assert "Other Cognee datasets you can search (active: agent_sessions)" in ctx
    assert f"  - project-alpha [{OTHER_ID}]" in ctx and f"  - team-notes [{THIRD_ID}]" in ctx
    assert "agent_sessions [" not in ctx  # the active dataset is never offered
    assert "--graph --dataset-id <id>" in ctx and "cognee-search.sh" in ctx
    assert run.detail("recall_dataset_hint") == {"active": "agent_sessions", "offered": 2}


def test_the_hint_never_reaches_the_header(lookup, monkeypatch):
    """The header is what the terminal shows; the hint is for the model only.
    (claude-code nests systemMessage in hookSpecificOutput; codex keeps it top-level.)"""
    _offer(lookup, monkeypatch)
    run = drive_recall(lookup, monkeypatch, recall=_MISS)
    header = run.output.get("systemMessage") or run.output["hookSpecificOutput"]["systemMessage"]
    assert header.startswith("Cognee memory") or "Cognee memory" in header
    assert "Other Cognee datasets" not in header


def test_hits_still_offer_the_other_datasets(lookup, monkeypatch):
    """Graph retrieval is nearest-neighbour and returns something from any
    populated dataset, so a hit is no proof the question was answered — the
    list rides along and the model judges."""
    _offer(lookup, monkeypatch)
    hit = dict(HIT, graph=[{"source": "graph", "content": "found something"}])
    run = drive_recall(lookup, monkeypatch, recall=hit)
    ctx = _context(run)
    assert "Relevant context from this session's memory" in ctx
    assert "Other Cognee datasets you can search" in ctx
    assert "does not answer it, do not conclude that memory has nothing" in ctx


def test_an_errored_graph_scope_still_offers_when_the_code_lane_answered(lookup, monkeypatch):
    """The server is alive (the code lane, the only other request the hook
    makes, answered), so the recommended search would work — the list is
    offered."""
    _offer(lookup, monkeypatch)
    arm_code_lane(monkeypatch)

    def recall(_prompt, **kw):
        if kw["scope"] == ["graph"]:
            raise urllib.error.HTTPError(URL, 500, "boom", {}, None)
        return []

    run = drive_recall(
        lookup, monkeypatch, recall=recall, prior_state={"state": "ready", "base_url": URL}
    )
    assert "Other Cognee datasets you can search" in _context(run)


def test_a_dead_server_offers_nothing(lookup, monkeypatch):
    """Nothing answered, so the search the hint recommends would fail the same
    way — no list, and the cache is not even consulted."""
    seen = _offer(lookup, monkeypatch)

    def recall(_prompt, **kw):
        raise ConnectionRefusedError("down")

    run = drive_recall(
        lookup, monkeypatch, recall=recall, prior_state={"state": "ready", "base_url": URL}
    )
    assert "Other Cognee datasets" not in _context(run)
    assert seen == [] and not run.fired("recall_dataset_hint")


def test_a_dataset_without_a_graph_yet_still_offers(lookup, monkeypatch):
    """404 on the graph scope is "no graph built yet" — an authoritative empty,
    from a server that answered; the other datasets are exactly what to offer."""
    _offer(lookup, monkeypatch)

    def recall(_prompt, **kw):
        if kw["scope"] == ["graph"]:
            raise urllib.error.HTTPError(URL, 404, "DatasetNotFound", {}, None)
        return []

    run = drive_recall(lookup, monkeypatch, recall=recall)
    assert run.fired("recall_graph_not_built")
    assert "Other Cognee datasets you can search" in _context(run)


def test_only_the_active_dataset_offers_nothing(lookup, monkeypatch):
    _offer(lookup, monkeypatch, rows=ROWS[:1])
    run = drive_recall(lookup, monkeypatch, recall=_MISS)
    assert _context(run).endswith("(no memory matches for this prompt)")
    assert not run.fired("recall_dataset_hint")


def test_the_hint_is_on_by_default(lookup, monkeypatch):
    """Unset, or any value that is not an explicit off, keeps the hint on."""
    _offer(lookup, monkeypatch)
    monkeypatch.delenv("COGNEE_RECALL_DATASET_HINT", raising=False)
    assert "Other Cognee datasets" in _context(drive_recall(lookup, monkeypatch, recall=_MISS))
    for value in ("on", "true", "1", "yes", ""):
        monkeypatch.setenv("COGNEE_RECALL_DATASET_HINT", value)
        assert "Other Cognee datasets" in _context(drive_recall(lookup, monkeypatch, recall=_MISS))


def test_knob_off_offers_nothing(lookup, monkeypatch):
    seen = _offer(lookup, monkeypatch)
    monkeypatch.setenv("COGNEE_RECALL_DATASET_HINT", "off")
    run = drive_recall(lookup, monkeypatch, recall=_MISS)
    assert "Other Cognee datasets" not in _context(run) and seen == []


def test_an_open_breaker_offers_nothing(lookup, monkeypatch):
    seen = _offer(lookup, monkeypatch)
    run = drive_recall(lookup, monkeypatch, recall=_MISS, breaker_open=(True, 30))
    assert "Other Cognee datasets" not in _context(run) and seen == []


def test_every_other_dataset_is_named_unranked(lookup, monkeypatch):
    """Nothing ranks the candidates, so the user is offered all of them."""
    many = ROWS[:1] + [
        {"name": f"ds-{i}", "id": f"{i:08d}-0000-4000-8000-000000000000"} for i in range(40)
    ]
    _offer(lookup, monkeypatch, rows=many)
    run = drive_recall(lookup, monkeypatch, recall=_MISS)
    ctx = _context(run)
    assert all(f"  - ds-{i} [{i:08d}-" in ctx for i in range(40))
    assert "more:" not in ctx
    assert run.detail("recall_dataset_hint")["offered"] == 40


def test_a_failing_lister_never_breaks_the_hook(lookup, monkeypatch):
    def boom(**kw):
        raise RuntimeError("cache exploded")

    monkeypatch.setattr(lookup, "cached_readable_datasets", boom)
    run = drive_recall(lookup, monkeypatch, recall=_MISS)
    assert _context(run).endswith("(no memory matches for this prompt)")
    assert run.detail("recall_error") == {
        "scope": ["dataset_hint"],
        "error": "cache exploded",
        "verdict": "unknown",
    }


def test_the_refresh_is_bounded_by_the_remaining_budget(lookup, monkeypatch):
    seen = _offer(lookup, monkeypatch)
    monkeypatch.setenv("COGNEE_RECALL_BUDGET", "12")
    drive_recall(lookup, monkeypatch, recall=_MISS)
    assert len(seen) == 1
    assert seen[0]["service_url"] == URL and seen[0]["refresh"] is True
    assert 0 < seen[0]["timeout"] <= 2.0


# ── list-datasets.py ───────────────────────────────────────────────────────


def test_active_dataset_follows_the_shell_wrappers_resolution(lister, monkeypatch):
    """One resolver for the wrappers and the lister: the launch record's
    dataset and UUIDs, an explicit --session-key honoured, and the launch-wide
    name fallback when the record carries none."""
    seen = []

    def overrides(service_url="", host_key=""):
        seen.append(host_key)
        return {
            "host_key": host_key or "found",
            "session_id": "s",
            "dataset": "" if host_key == "bare" else "ds",
            "dataset_id": ACTIVE_ID,
            "dataset_ids": f"{ACTIVE_ID},{OTHER_ID}",
            "api_key": "",
        }

    monkeypatch.setattr(lister, "shell_runtime_overrides", overrides)
    monkeypatch.setattr(lister, "resolve_active_dataset", lambda key: f"fallback-for-{key}")
    assert lister.active_dataset() == {"name": "ds", "id": ACTIVE_ID, "ids": [ACTIVE_ID, OTHER_ID]}
    assert lister.active_dataset("bare")["name"] == "fallback-for-bare"
    assert seen == ["", "bare"]


@pytest.fixture
def lister(suite, hook_module, monkeypatch):
    return hook_module(suite, "list-datasets.py")


def test_listing_marks_the_active_dataset(lister):
    current = {"name": "agent_sessions", "id": ACTIVE_ID, "ids": [ACTIVE_ID]}
    out = lister.build_listing(current, list(ROWS))
    assert out["current"] == current
    assert [(r["name"], r["current"]) for r in out["datasets"]] == [
        ("agent_sessions", True),
        ("project-alpha", False),
        ("team-notes", False),
    ]
    # The script path is quoted so a plugin root containing a space survives the
    # shell the model runs this in (topoteretes/cognee#5154).
    assert out["search"].endswith('cognee-search.sh" "<query>" 10 --graph --dataset-id <id>')
    assert out["search"].startswith('"')


def test_listing_others_drops_the_active_dataset(lister):
    current = {"name": "agent_sessions", "id": "", "ids": []}
    out = lister.build_listing(current, list(ROWS), others_only=True)
    assert [r["name"] for r in out["datasets"]] == ["project-alpha", "team-notes"]


def test_main_prints_json_and_reports_server_failure(lister, monkeypatch, capsys):
    monkeypatch.setattr(lister, "active_dataset", lambda key="": {"name": "a", "id": "", "ids": []})
    monkeypatch.setattr(lister, "list_readable_datasets", lambda: list(ROWS))
    monkeypatch.setattr(lister, "hook_log", lambda *a, **k: None)
    assert lister.main(["--json", "--others"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in out["datasets"]] == ["agent_sessions", "project-alpha", "team-notes"]
    assert "search" in out

    def boom():
        raise urllib.error.HTTPError(URL, 401, "nope", {}, None)

    monkeypatch.setattr(lister, "list_readable_datasets", boom)
    assert lister.main(["--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "error": "GET /api/v1/datasets failed (HTTP 401)",
        "code": 1,
    }
