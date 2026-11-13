"""sync-session-to-graph.py run from the agent's shell, outside any hook.

``cognee-forget.sh sync`` and the cognee-sync skill run the sync from Claude
Code's Bash tool: no hook payload on stdin and no ``COGNEE_SESSION_KEY``. The
sync must still find the launch it belongs to (the way ``switch-dataset.py``
does) instead of minting a fresh session id — in cloud mode that empty key
reached ``session_dir("")`` and the command died with ``unsafe session id``
(#559).
"""

from __future__ import annotations

import importlib
import io
import os
import sys
from pathlib import Path

import pytest

HOST = "host-shell-sync-1"


def test_shell_sync_resolves_the_live_launch(suite, hook_module, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("only the Claude Code plugin has the cloud sync path")

    # Every loader pops the suite modules first, so take the siblings main()
    # imports lazily from the interpreter the hook module was loaded into.
    sync = hook_module(suite, "sync-session-to-graph.py")
    pc = sys.modules["_plugin_common"]
    worker = importlib.import_module("_cloud_worker")
    for module in (pc, sync):
        monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)

    # The launch as SessionStart left it; the shell only carries Claude's own id.
    monkeypatch.delenv("COGNEE_SESSION_KEY", raising=False)
    monkeypatch.delenv("COGNEE_SESSION_ID", raising=False)
    monkeypatch.delenv("COGNEE_SYNC_SESSION_ID", raising=False)
    monkeypatch.delenv("COGNEE_SYNC_DATASET", raising=False)
    session_id, _conn = pc.ensure_launch_record(HOST, "/w", dataset="a")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", HOST)

    requested: list[tuple[str, str, str]] = []
    monkeypatch.setattr(worker, "enabled", lambda: True)
    monkeypatch.setattr(
        worker,
        "request_improve",
        lambda dataset, sid, reason="stop": (
            requested.append((dataset, sid, reason)) or Path("unused")
        ),
    )

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "argv", ["sync-session-to-graph.py"])

    sync.main()  # must not raise

    assert requested == [("a", session_id, "manual")]
    assert os.environ["COGNEE_SESSION_KEY"] == HOST
