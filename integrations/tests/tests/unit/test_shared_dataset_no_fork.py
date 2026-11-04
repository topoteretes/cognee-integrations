"""Under shared memory the plugin never forks an agent-owned copy of its dataset.

The server's ``POST /api/v1/datasets`` create-or-return matches a name only
among the CALLER's datasets. The pre-improve ensure posted the launch's dataset
by name with the agent key, so on an agent that owned no dataset of that name
the server created an empty agent-owned ``agent_sessions`` beside the canonical,
parent-owned one SessionStart had resolved. The listing then marked both rows
current (it matched by name), and the Claude Code picker, offered the same name
twice, was refused by the engine and the whole status row vanished.

Covers, for every suite:
  * the ensure skips the by-name POST when the name has a canonical id, and
    still posts it when it has none (name-addressed launches)
  * ``switch-dataset.py --list`` marks only the launch's canonical row current
    when a same-named dataset of another principal is listed beside it
"""

from __future__ import annotations

import pytest

CANONICAL = "5708f668-e272-5475-9341-caac1f6ce9f9"
AGENT_COPY = "6eff268f-a66d-5d1c-a79d-fb4a1c44254e"


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


def _record_posts(pc, monkeypatch) -> list:
    posts: list = []
    monkeypatch.setattr(
        pc, "_json_http_request", lambda path, body, **kw: posts.append((path, body)) or {}
    )
    return posts


def test_ensure_skips_the_by_name_create_for_a_canonical_dataset(pc, monkeypatch):
    posts = _record_posts(pc, monkeypatch)
    monkeypatch.setattr(pc, "dataset_id_for", lambda name, host_key="": CANONICAL)
    pc.ensure_dataset_via_http("agent_sessions")
    assert posts == []


def test_ensure_still_creates_a_name_addressed_dataset(pc, monkeypatch):
    posts = _record_posts(pc, monkeypatch)
    monkeypatch.setattr(pc, "dataset_id_for", lambda name, host_key="": "")
    pc.ensure_dataset_via_http("agent_sessions")
    assert posts == [("/api/v1/datasets", {"name": "agent_sessions"})]


@pytest.fixture
def switch(suite, hook_module, monkeypatch):
    module = hook_module(suite, "switch-dataset.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(module, "load_resolved", lambda **kw: {"user_id": "agent"})
    monkeypatch.setattr(module, "recall_mode", lambda host_key: ("auto", ""))
    monkeypatch.setattr(
        module,
        "list_writable_datasets",
        lambda user_id="": {
            "datasets": [
                {"name": "agent_sessions", "id": CANONICAL, "writable": True},
                {"name": "agent_sessions", "id": AGENT_COPY, "writable": True},
                {"name": "demo", "id": "d2", "writable": True},
            ],
            "hidden_readonly": 0,
            "filtered": True,
        },
    )
    return module


def test_list_marks_only_the_canonical_row_current(switch):
    rec = {"dataset": "agent_sessions", "dataset_id": CANONICAL, "session_id": "s1"}
    rows = switch._list("host-1", rec)["datasets"]
    assert [row["id"] for row in rows if row["current"]] == [CANONICAL]


def test_list_without_a_canonical_id_matches_by_name(switch):
    rec = {"dataset": "demo", "session_id": "s1"}
    rows = switch._list("host-1", rec)["datasets"]
    assert [row["id"] for row in rows if row["current"]] == ["d2"]
