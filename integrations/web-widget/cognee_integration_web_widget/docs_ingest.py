"""Turn documentation pages into the text the widget answers from.

The corpus was originally built by a script that is not in any repository, so
this reproduces what the stored content shows rather than inheriting it: the
frontmatter becomes a heading and a summary line, a Source line records where
the page lives, and MDX component tags are dropped so answers quote prose rather
than JSX.

Citation links do not depend on this - they are built from the item name - so
the transform only has to produce good text, not match the original byte for
byte.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
_FIELD = re.compile(r'^(title|description):\s*["\']?(.*?)["\']?\s*$', re.M)
# <Note>, </Note>, <Card ... />, <Frame> - tags only; their text is kept.
_MDX_TAG = re.compile(r"</?[A-Z][A-Za-z0-9]*(?:\s[^<>]*?)?/?>")
_IMPORTS = re.compile(r"^import\s+.*$", re.M)

DEFAULT_EXCLUDES = (".github", ".mintlify-eval", "node_modules", "snippets")


def item_name(relative_path: str) -> str:
    """The flattened name the corpus uses: a/b/c.mdx -> a__b__c."""
    stem = re.sub(r"\.(mdx|md)$", "", relative_path)
    return stem.replace("/", "__")


def to_document(source: str, relative_path: str, docs_url: Optional[str] = None) -> str:
    """Render one page as the plain text that gets ingested."""
    title = description = ""
    match = _FRONTMATTER.match(source)
    body = source
    if match:
        for key, value in _FIELD.findall(match.group(1)):
            if key == "title":
                title = value.strip()
            elif key == "description":
                description = value.strip()
        body = source[match.end() :]

    body = _IMPORTS.sub("", body)
    body = _MDX_TAG.sub("", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    head = []
    if title:
        head.append(f"# {title}")
    if description:
        head.append(description)
    if docs_url:
        page = re.sub(r"\.(mdx|md)$", "", relative_path)
        head.append(f"(Source: {docs_url.rstrip('/')}/{page})")
    return "\n\n".join([*head, body]).strip() + "\n"


def list_pages(root: Path, excludes: tuple = DEFAULT_EXCLUDES) -> list[dict]:
    """Every documentation page under root, newest-irrelevant, sorted by path.

    Files under an excluded directory are listed but not selected by default -
    hiding them would silently decide what the corpus contains.
    """
    pages = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".mdx", ".md") or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        top = relative.split("/")[0]
        excluded = top in excludes or relative.startswith(".")
        pages.append(
            {
                "path": relative,
                "name": item_name(relative),
                "folder": relative.rsplit("/", 1)[0] if "/" in relative else "(root)",
                "bytes": path.stat().st_size,
                "recommended": not excluded,
            }
        )
    return pages
