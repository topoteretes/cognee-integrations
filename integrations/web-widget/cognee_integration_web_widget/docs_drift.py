"""Has the documentation changed since it was ingested?

The corpus carries no content hash, and ``updatedAt`` moves whenever cognee
reprocesses a record - a dataset-wide re-cognify bumps all of them while no
document has been touched. So neither field can answer the question.

What can: the source repository. Each item's ``name`` encodes its path
(``setup-configuration__llm-providers`` -> ``setup-configuration/llm-providers``),
so an item can be matched to a file, and that file's last commit compared with
``createdAt``, which is stable across reprocessing.

This needs to see the repository, so it is opt-in via ``WIDGET_DOCS_PATH`` and
reports nothing at all when unset - a hosted backend cannot do this, and a badge
that guesses is worse than no badge.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

_EXTENSIONS = (".mdx", ".md")


def _repo_file(root: Path, name: str) -> Optional[Path]:
    """The documentation file an ingested item came from, if it still exists."""
    relative = name.replace("__", "/")
    for extension in _EXTENSIONS:
        candidate = root / (relative + extension)
        if candidate.is_file():
            return candidate
    return None


def last_commit_dates(root: Path) -> dict[str, str]:
    """Every tracked file's most recent commit date, in one pass.

    Asking git per file costs a process each - 251 of them for this corpus. One
    ``git log`` walk newest-first gives the same answer: the first time a path
    appears is its latest commit.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--format=%x00%cI", "--name-only", "--no-renames"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}

    dates: dict[str, str] = {}
    current = ""
    for line in result.stdout.splitlines():
        if line.startswith("\x00"):
            current = line[1:].strip()
        elif line.strip() and current:
            dates.setdefault(line.strip(), current)
    return dates


def drift_for_items(items: list, docs_path: Optional[str]) -> dict:
    """Map each item to its source file and compare commit date with ingest.

    Returns ``{item_id: True/False}`` - True meaning the documentation has been
    edited since that item was ingested - plus how many items could be matched
    at all, so the caller can distinguish "nothing has drifted" from "nothing
    could be checked".
    """
    if not docs_path:
        return {"enabled": False, "states": {}, "matched": 0, "drifted": 0}

    root = Path(docs_path).expanduser()
    if not root.is_dir():
        return {"enabled": False, "states": {}, "matched": 0, "drifted": 0}

    dates = last_commit_dates(root)
    states: dict[str, bool] = {}
    matched = drifted = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        created = str(item.get("createdAt") or "")
        file = _repo_file(root, name) if name else None
        if not file or not created:
            continue
        committed = dates.get(str(file.relative_to(root)))
        if not committed:
            # Tracked path with no commit touching it, or an untracked file:
            # nothing to compare against, so make no claim.
            continue
        matched += 1
        is_drifted = committed > created
        states[str(item.get("id"))] = is_drifted
        drifted += is_drifted

    return {"enabled": True, "states": states, "matched": matched, "drifted": drifted}
