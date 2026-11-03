"""Cross-dataset search: a one-off graph search of one named dataset.

Recall is auto by default (SDK-1393: the server routes each query to the best
dataset) or pinned to the launch's dataset, so the prompt hook no longer lists
the other datasets. The skills still name them through ``list-datasets.py``,
and ``cognee-search.sh --dataset-id`` reads one without switching.

Contract:
  * ``list_readable_datasets`` is the read set exactly as ``GET /api/v1/datasets``
    returns it — no permission calls, read-only rows included
    (``list_writable_datasets`` builds on it; its own tests live in
    test_dataset_switch.py);
  * ``other_readable_datasets`` drops the active dataset by every handle it
    goes by (its UUIDs under shared memory, its name when name-addressed);
  * the prompt hook never appends an "other datasets" block;
  * ``list-datasets.py`` marks the active dataset and can drop it.
"""

from __future__ import annotations

import json
import urllib.error

import pytest
from utils.recall import HIT, SCOPES, URL, drive_recall, load_lookup


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


# ── the prompt hook ────────────────────────────────────────────────────────


@pytest.fixture
def lookup(suite, hook_module, monkeypatch):
    module = load_lookup(suite, hook_module, monkeypatch)
    monkeypatch.setattr(module, "resolve_active_dataset_ids", lambda *a, **k: ("", []))
    monkeypatch.setattr(module, "get_dataset", lambda config: "agent_sessions")
    return module


def _context(run) -> str:
    return (run.output or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


@pytest.mark.parametrize("auto_route", [True, False])
def test_the_hook_never_offers_other_datasets(lookup, monkeypatch, auto_route):
    """SDK-1393: recall is auto (routed server-side) unless the user pinned a
    dataset, so the prompt hook no longer lists other datasets or asks the
    user to pick one, hit or miss."""
    for recall in (_MISS, {**_MISS, "graph": [HIT]}):
        ctx = _context(drive_recall(lookup, monkeypatch, recall=recall, auto_route=auto_route))
        assert "Other Cognee datasets" not in ctx
        assert "AskUserQuestion" not in ctx


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
