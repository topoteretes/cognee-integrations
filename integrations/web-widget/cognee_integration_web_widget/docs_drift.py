"""Has the documentation changed since it was ingested?

The corpus answers this itself, once you know where to look. cognee stores every
upload at a content-addressed path, so an item's ``rawDataLocation`` ends in
``text_<md5>.txt`` - the digest of the exact bytes it holds. Render a page the
way ingest would render it today, hash that, and the two either agree or they do
not. No proxy, no inference.

The fields that look like they should answer this cannot. ``updatedAt`` moves
whenever cognee reprocesses a record, so a dataset-wide re-cognify reports every
document as changed while nobody has touched a page, and ``createdAt`` says when
an item arrived rather than what is in it.

This replaces a comparison of each file's last commit date against ``createdAt``.
That was a proxy for content, and it was wrong in three directions at once: an
uncommitted edit read as current, a revert or a reformat read as edited, and a
source folder that was not a git repository could not be read at all. Only one
question still goes to git - whether a path it no longer finds ever existed -
and the answer degrades to "not documentation" when there is no repository.

Comparing needs the source files, so it stays opt-in via ``WIDGET_DOCS_PATH`` and
reports nothing at all when unset: a badge that guesses is worse than no badge.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Optional

from .docs_ingest import render_for_ingest

_EXTENSIONS = (".mdx", ".md")

# s3://…/text_b4cb18fd4f75db55c8773c9e4ffb6a13.txt
_STORED_DIGEST = re.compile(r"text_([0-9a-f]{32})(?:\.[A-Za-z0-9]+)?$")


def stored_digest(item: dict) -> Optional[str]:
    """The digest cognee's own storage path carries for this item.

    ``None`` when the location is shaped some other way. That is a reason to say
    nothing about the item, not to assume it is current: an unreadable location
    and an unchanged page are different states.
    """
    if not isinstance(item, dict):
        return None
    match = _STORED_DIGEST.search(str(item.get("rawDataLocation") or ""))
    return match.group(1) if match else None


def content_digest(text: str) -> str:
    """The digest cognee would store for ``text``.

    MD5 because that is what cognee's storage path uses; this is a checksum for
    telling two versions of a page apart, never a security claim.
    """
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def _candidate_paths(name: str) -> list:
    """The paths an item's name could have come from, likeliest first.

    ``item_name`` strips ``.md`` and ``.mdx`` and no other suffix, so a
    documentation page arrives here without its extension while every other
    file - a .py, a .yml, a .txt - arrives with it. Trying only the two
    markdown spellings meant no non-markdown file was ever matched to its
    source: it read as "not from docs" while sitting in the docs folder.
    """
    relative = name.replace("__", "/")
    return [relative] + [relative + extension for extension in _EXTENSIONS]


def _source_file(root: Path, name: str) -> Optional[Path]:
    """The file an ingested item came from, if it still exists."""
    for candidate in _candidate_paths(name):
        path = root / candidate
        if path.is_file():
            return path
    return None


def _paths_git_has_seen(root: Path) -> set:
    """Every path in the history, including ones since deleted.

    Only used to tell a page that was deleted from the docs apart from an item
    that was never documentation. Asked once, and only when some item has no
    file; an empty set - no repository, no git, a failed call - collapses both
    cases into "not documentation", which understates the problem rather than
    inventing one.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--format=", "--name-only", "--no-renames"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def drift_for_items(items: list, docs_path: Optional[str], docs_url: Optional[str] = None) -> dict:
    """Classify every ingested item against the documentation it came from.

    Five outcomes:

    ``current``  the page renders to exactly the bytes the corpus holds
    ``edited``   it does not - the page has changed since it was ingested
    ``removed``  git knows the path but the file is gone - the page was deleted
                 from the docs while its content stayed in the corpus, so the
                 widget can still answer from it and cite a URL that 404s
    ``foreign``  no file, and git has never seen the path - not documentation at
                 all, which is what the demo seeds are
    ``unknown``  the page is there but the item carries no digest to compare it
                 against, so nothing can be claimed either way

    ``docs_url`` must be the one ingest stamps into the Source line, or every
    page renders to different bytes than were stored and the whole corpus reads
    as edited.
    """
    empty = {"enabled": False, "states": {}, "matched": 0, "drifted": 0, "removed": 0}
    if not docs_path:
        return empty

    root = Path(docs_path).expanduser()
    if not root.is_dir():
        return empty

    states: dict[str, str] = {}
    matched = drifted = removed = 0
    absent: list[tuple[str, str]] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        item_id = str(item.get("id"))
        if not name:
            continue

        file = _source_file(root, name)
        if file is None:
            absent.append((item_id, name))
            continue

        stored = stored_digest(item)
        if not stored:
            states[item_id] = "unknown"
            continue

        relative = file.relative_to(root).as_posix()
        # Read exactly as ingest reads, or the bytes differ for a reason that
        # has nothing to do with the page having been edited.
        source = file.read_text(encoding="utf-8", errors="replace")
        matched += 1
        if content_digest(render_for_ingest(source, relative, docs_url)) == stored:
            states[item_id] = "current"
        else:
            states[item_id] = "edited"
            drifted += 1

    if absent:
        seen = _paths_git_has_seen(root)
        for item_id, name in absent:
            # Same spellings the lookup above tried, or a deleted .py would be
            # called foreign for the reason a present one used to be.
            if any(candidate in seen for candidate in _candidate_paths(name)):
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
