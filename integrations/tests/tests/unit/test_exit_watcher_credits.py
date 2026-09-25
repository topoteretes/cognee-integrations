"""The exit-watcher makes no credits calls.

It used to host a session-long credits poll (every 5 minutes while the host
was alive) so the status-line balance would not age out of the renderer's
15-minute TTL during idle stretches. That poll was the plugin's only idle
network traffic — one billing fetch per open terminal per interval — and its
throttle read the marker entry's ``checked_at``, which does not exist until a
refresh has SUCCEEDED for our tenant: with no tenant binding (self-hosted
remote server, unresolved connection lookup) the gate failed open into a
refresh attempt per 2-second poll and ~14k ``credits_refresh_skipped_no_tenant``
lines a day.

The balance cannot move from this machine while it is idle, so the poll is
gone: the hook-time refreshes (prompt start, turn end, remember, improve) are
the whole cadence, and the renderer shows an old reading's age instead of
hiding it (see test_statusline_credits). These pin that the watcher stays out
of the credits business.
"""

from __future__ import annotations

import ast
from pathlib import Path


def _watcher_source(suite) -> str:
    return (suite.scripts_dir / "exit-watcher.py").read_text(encoding="utf-8")


def test_watcher_has_no_credits_refresh_helper(suite, hook_module, temp_home):
    watcher = hook_module(suite, "exit-watcher.py")
    assert not hasattr(watcher, "_refresh_credits_marker")
    assert not hasattr(watcher, "_last_credits_attempt_at")


def test_watcher_never_imports_or_calls_refresh_credits(suite):
    """Static: no path in the watcher reaches ``refresh_credits`` — not the
    loop body, not a late function-local import."""
    src = _watcher_source(suite)
    assert "refresh_credits" not in src
    assert "COGNEE_CREDITS_CHECK_INTERVAL" not in src


def test_watcher_loop_only_reprobes_connection(suite):
    """The steady-state loop body is the connection self-heal plus the sleep;
    nothing else runs per poll."""
    tree = ast.parse(_watcher_source(suite))
    loops = [n for n in ast.walk(tree) if isinstance(n, ast.While)]
    assert len(loops) == 1, "expected exactly one steady-state loop"
    calls = [
        n.func.id if isinstance(n.func, ast.Name) else ast.unparse(n.func)
        for stmt in loops[0].body
        for n in ast.walk(stmt)
        if isinstance(n, ast.Call)
    ]
    assert calls == ["_reprobe_connection", "time.sleep"], calls


def test_credits_check_error_event_is_retired(suite, isolated_modules):
    events = isolated_modules(suite, "event_names")
    assert "exit-watcher:credits_check_error" not in events.EVENT_NAMES
    doc = (Path(events.__file__).parent.parent / "EVENTS.md").read_text(encoding="utf-8")
    assert "credits_check_error" not in doc
