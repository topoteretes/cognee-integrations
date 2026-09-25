"""Reading a suite's ``hook.log`` back as ``(event, detail)`` pairs.

Every hook appends one JSON object per line to ``<state_dir>/hook.log``; the
tests (unit-driven, e2e subprocess, and live alike) assert on that record. One
reader, so no test file grows its own copy of the parse loop.
"""

from __future__ import annotations

import json
from pathlib import Path

from .suites import Suite, state_dir


def hook_events(suite: Suite, home: Path) -> list[tuple[str, dict]]:
    """Every (event, detail) the hooks have logged so far, in order.

    Unparseable lines are skipped rather than raised: a rotation or a partial
    write mid-read must not turn into a test failure about JSON.
    """
    path = state_dir(suite, home) / "hook.log"
    if not path.exists():
        return []
    events: list[tuple[str, dict]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except Exception:
            continue
        events.append((str(entry.get("event", "")), entry.get("detail") or {}))
    return events
