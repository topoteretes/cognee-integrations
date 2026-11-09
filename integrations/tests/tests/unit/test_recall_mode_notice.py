"""Telling an upgrading user that recall is auto (SDK-1393).

Recall is auto by default, so a configured non-default ``COGNEE_PLUGIN_DATASET``
no longer restricts it. Two surfaces say so:

  * the status shows the mode in effect: ``auto (<dataset>)`` while recall is
    auto, the bare name while it is pinned, in every hook-family host (the
    status line script works the mode out from local state, the hooks pass
    ``auto_recall_active``);
  * session start says it once per machine and dataset, in the message the user
    sees (``systemMessage``), only when a non-default dataset is configured,
    ``COGNEE_PLUGIN_RECALL_MODE`` was never set and the session has not pinned
    recall itself.
"""

from __future__ import annotations

import json

import pytest

HOST = "host-notice-1"
DATASET = "project_x"


# ── the status ─────────────────────────────────────────────────────────────


def _plain_status(statusline, monkeypatch):
    if not hasattr(statusline, "render_status_for_host"):
        pytest.skip("Claude Code puts no status line in the context it injects")
    monkeypatch.setattr(statusline, "_active_dataset", lambda host_id="": DATASET)
    monkeypatch.setattr(statusline, "_status_prefix", lambda host_id="": "● ")
    monkeypatch.setattr(statusline, "_active_mode", lambda: "local")
    if hasattr(statusline, "_mode_label"):
        monkeypatch.setattr(statusline, "_mode_label", lambda: "local")
    monkeypatch.setattr(statusline, "_credits_segment", lambda: "")
    monkeypatch.setattr(statusline, "_update_segment", lambda: "")
    return statusline


def test_status_reads_auto_while_recall_is_auto(statusline, monkeypatch):
    status = _plain_status(statusline, monkeypatch)
    assert status.render_status_for_host(HOST, True) == f"● cognee: auto ({DATASET}) · local"
    assert status.render_status_for_host(HOST, False) == f"● cognee: {DATASET} · local"
    assert status.render_status_for_host(HOST) == f"● cognee: {DATASET} · local"


def _record(suite, temp_home, **fields):
    sessions = temp_home / ".cognee-plugin" / suite.state_subdir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{HOST}.json").write_text(
        json.dumps({"session_id": "s", "dataset": DATASET, **fields}), encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("record", "env", "auto"),
    [
        ({}, {}, True),
        ({}, {"COGNEE_PLUGIN_RECALL_MODE": "pinned"}, False),
        ({}, {"COGNEE_PLUGIN_RECALL_MODE": "junk"}, True),
        ({"recall_pinned": True}, {}, False),
        ({"recall_pinned": False}, {"COGNEE_PLUGIN_RECALL_MODE": "pinned"}, True),
        ({"switched_at": "2026-01-01"}, {}, False),
        ({}, {"COGNEE_PLUGIN_READ_DATASET_IDS": "11111111-1111-4111-8111-111111111111"}, False),
    ],
)
def test_the_status_line_works_out_the_mode_as_the_hooks_do(
    suite, statusline, isolated_modules, temp_home, monkeypatch, record, env, auto
):
    """The status line script imports no ``_plugin_common``: its own reading of
    the recall mode must agree with ``auto_recall_active``."""
    pc = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(pc, "hook_log", lambda *a, **k: None)
    for name in ("COGNEE_PLUGIN_RECALL_MODE", "COGNEE_PLUGIN_READ_DATASET_IDS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _record(suite, temp_home, **record)
    assert pc.auto_recall_active(HOST) is auto
    assert statusline._recall_auto(HOST) is auto
    label = statusline._dataset_label(HOST, statusline._recall_auto(HOST))
    assert label == (f"auto ({DATASET})" if auto else DATASET)


# ── the once-only notice ───────────────────────────────────────────────────


@pytest.fixture
def start(suite, hook_module, monkeypatch):
    module = hook_module(suite, "session-start.py")
    import _plugin_common

    events: list = []
    monkeypatch.setattr(module, "hook_log", lambda e, d=None: events.append(e))
    monkeypatch.setattr(module, "get_session_key", lambda: HOST)
    monkeypatch.setattr(_plugin_common, "hook_log", lambda *a, **k: None)
    for name in ("COGNEE_PLUGIN_RECALL_MODE", "COGNEE_PLUGIN_READ_DATASET_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", DATASET)
    module.events = events
    return module


STARTUP = {"source": "startup"}


def test_notice_shows_once_for_a_configured_dataset(start):
    out = start._apply_recall_mode_notice({"systemMessage": "● cognee: ready"}, STARTUP)
    message = out["systemMessage"]
    assert message.startswith("● cognee: ready\n\nCognee: recall now searches every dataset")
    assert f"not only `{DATASET}` (saves still go there)" in message
    assert "set `COGNEE_PLUGIN_RECALL_MODE=pinned`, or ask to pin recall" in message
    assert "To search one dataset once, name it in your request." in message
    # What the user sees, not the agent's context.
    assert "additionalContext" not in out.get("hookSpecificOutput", {})
    assert start.events == ["recall_mode_notice_shown"]
    assert start._apply_recall_mode_notice({}, STARTUP) == {}  # once per machine and dataset


def test_notice_shows_again_for_another_dataset(start, monkeypatch):
    start._apply_recall_mode_notice({}, STARTUP)
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", "project_y")
    assert "`project_y`" in start._apply_recall_mode_notice({}, STARTUP)["systemMessage"]


@pytest.mark.parametrize("mode", ["auto", "pinned"])
def test_no_notice_when_a_recall_mode_is_set(start, monkeypatch, mode):
    monkeypatch.setenv("COGNEE_PLUGIN_RECALL_MODE", mode)
    assert start._apply_recall_mode_notice({}, STARTUP) == {}


@pytest.mark.parametrize("dataset", ["agent_sessions", "auto", ""])
def test_no_notice_for_the_default_dataset(start, monkeypatch, dataset):
    monkeypatch.setenv("COGNEE_PLUGIN_DATASET", dataset)
    assert start._apply_recall_mode_notice({}, STARTUP) == {}


def test_no_notice_when_the_session_pinned_recall(start, suite, temp_home):
    _record(suite, temp_home, recall_pinned=True)
    assert start._apply_recall_mode_notice({}, STARTUP) == {}


def test_no_notice_on_a_compaction(start):
    assert start._apply_recall_mode_notice({}, {"source": "compact"}) == {}
    assert start._apply_recall_mode_notice({}, STARTUP)["systemMessage"]  # still due
