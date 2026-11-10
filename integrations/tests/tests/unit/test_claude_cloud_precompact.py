"""Cloud mode still prints a pre-compact anchor (SDK-1492).

The cloud branch used to queue an improve for the background worker and return,
so a compaction in cloud mode dropped the transcript with nothing to carry the
session across it. The improve is still queued; the anchor is built as well.
"""

from __future__ import annotations

import asyncio
import sys

import pytest


@pytest.fixture
def compact(suite, hook_module, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("Cloud worker is claude-code only")
    module = hook_module(suite, "pre-compact.py")
    monkeypatch.setattr(module, "_load_resolved_fields", lambda: ("claude_sid", "ds"))
    monkeypatch.setattr(
        module, "resolve_runtime_mode", lambda: {"base_url": "https://cloud.example"}
    )
    monkeypatch.setattr(module, "cloud_enabled", lambda *a, **kw: True)
    # The cloud worker refreshes the ready hint while a session is active.
    monkeypatch.setattr(module, "server_usable", lambda *a, **kw: True)
    monkeypatch.setattr(
        module,
        "get_session_detail_via_http",
        lambda session_id, **kw: {
            "qas": [{"question": "Where does orion-7 deploy?", "answer": "edge-7"}],
            "traces": [],
        },
    )
    monkeypatch.setattr(module, "recall_via_http", lambda *a, **kw: [])
    return module


def _improves(monkeypatch, *, fail: bool = False) -> list:
    requested = []

    def request_improve(dataset, session_id, reason="stop"):
        requested.append((dataset, session_id, reason))
        if fail:
            raise OSError("improve request busy")

    monkeypatch.setattr(sys.modules["_cloud_worker"], "request_improve", request_improve)
    return requested


def test_cloud_precompact_queues_the_improve_and_prints_the_anchor(compact, monkeypatch, capsys):
    requested = _improves(monkeypatch)
    asyncio.run(compact._run())
    anchor = capsys.readouterr().out
    assert requested == [("ds", "claude_sid", "precompact")]
    assert "Cognee Memory Anchor" in anchor
    assert "orion-7" in anchor


def test_a_busy_improve_request_does_not_cost_the_anchor(compact, monkeypatch, capsys):
    requested = _improves(monkeypatch, fail=True)
    asyncio.run(compact._run())
    assert requested, "the improve was not even attempted"
    assert "orion-7" in capsys.readouterr().out


def test_an_unusable_cloud_server_still_gets_the_improve_but_no_anchor(
    compact, monkeypatch, capsys
):
    """Same gate as local mode: a server the worker marked down is not waited on."""
    requested = _improves(monkeypatch)
    monkeypatch.setattr(compact, "server_usable", lambda *a, **kw: False)
    monkeypatch.setattr(
        compact,
        "get_session_detail_via_http",
        lambda *a, **kw: pytest.fail("fetched session detail from an unusable server"),
    )
    asyncio.run(compact._run())
    assert requested == [("ds", "claude_sid", "precompact")]
    assert capsys.readouterr().out == ""
