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
    """Classify every ingested item against the documentation repository.

    Four outcomes, because "no status" was hiding two different problems:

    ``current``  the page exists and has no commit since it was ingested
    ``edited``   the page exists and has been committed since - reingest it
    ``removed``  git knows the path but the file is gone - the page was deleted
                 from the docs while its content stayed in the corpus, so the
                 widget can still answer from it and cite a page that 404s
    ``foreign``  git has never seen the path - not documentation at all, which
                 is what the demo seeds are

    The single ``git log`` walk already lists deleted paths (they appear in the
    commit that removed them), so telling ``removed`` from ``foreign`` costs
    nothing extra.
    """
    empty = {"enabled": False, "states": {}, "matched": 0, "drifted": 0, "removed": 0}
    if not docs_path:
        return empty

    root = Path(docs_path).expanduser()
    if not root.is_dir():
        return empty

    dates = last_commit_dates(root)
    states: dict[str, str] = {}
    matched = drifted = removed = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        created = str(item.get("createdAt") or "")
        item_id = str(item.get("id"))
        if not name or not created:
            continue

        file = _repo_file(root, name)
        if file:
            committed = dates.get(str(file.relative_to(root)))
            if not committed:
                # Untracked file: present but never committed, so there is no
                # date to compare against.
                states[item_id] = "foreign"
                continue
            matched += 1
            if committed > created:
                states[item_id] = "edited"
                drifted += 1
            else:
                states[item_id] = "current"
            continue

        # No file. Did one ever exist at that path?
        relative = name.replace("__", "/")
        known = any(f"{relative}{extension}" in dates for extension in _EXTENSIONS)
        if known:
            states[item_id] = "removed"
            removed += 1
        else:
            states[item_id] = "foreign"

    return {
        "enabled": True,
        "states": states,
        "matched": matched,
        "drifted": drifted,
        "removed": removed,
    }
