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
from typing import Optional

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
_FIELD = re.compile(r'^(title|description):\s*["\']?(.*?)["\']?\s*$', re.M)
# <Note>, </Note>, <Card ... />, <Frame> - tags only; their text is kept.
_MDX_TAG = re.compile(r"</?[A-Z][A-Za-z0-9]*(?:\s[^<>]*?)?/?>")
_IMPORTS = re.compile(r"^import\s+.*$", re.M)

MARKDOWN_SUFFIXES = (".md", ".mdx")


def item_name(relative_path: str) -> str:
    """The flattened name the corpus uses: a/b/c.mdx -> a__b__c."""
    stem = re.sub(r"\.(mdx|md)$", "", relative_path)
    return stem.replace("/", "__")


def render_for_ingest(source: str, relative_path: str, docs_url: Optional[str] = None) -> str:
    """The exact text that gets ingested for ``relative_path``.

    Markdown goes through the MDX transform below; anything else is stored as
    it stands. That transform drops tags shaped like JSX components, which is
    right for a documentation page and wrong for any other file that happens to
    contain angle brackets.

    Both ingest and drift go through here. Drift decides "edited" by hashing
    what would be ingested now and comparing it with what was stored, so the
    two rendering one file differently would read as the file having changed.
    """
    if relative_path.lower().endswith(MARKDOWN_SUFFIXES):
        return to_document(source, relative_path, docs_url)
    return source


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
