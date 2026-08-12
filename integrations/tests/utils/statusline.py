"""Suite-aware expectations for the status-line renderer.

The two renderers are the same logic with one deliberate difference: claude-code
styles the bar with ANSI escapes for a terminal, while codex's string is injected
into the model's context and must stay plain text. Tests that care about *which*
glyph or segment appears are shared and go through these helpers; tests that care
about the styling itself are per-suite (and skip on the other suite).

Both renderers derive every marker path from ``Path.home()``, so a module loaded
through ``isolated_modules`` already reads and writes inside the per-test HOME —
no path constants need patching. Marker payload shapes differ per segment, so
each test file builds its own; ``write_json`` is the one shared piece.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_ANSI = re.compile(r"\033\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """The bar's semantic content, with any styling removed."""
    return _ANSI.sub("", text)


def ok_glyph(sl) -> str:
    """The healthy-dot glyph this suite renders, including its trailing space."""
    helper = getattr(sl, "_ok_glyph", None)
    return helper() if helper else "● "


def reason_label(sl, reason: str) -> str:
    """The user-facing label for an internal marker state (e.g. auth_failed)."""
    return sl._REASON_LABELS.get(reason, reason)


def fail_glyph(sl, state: str) -> str:
    """The ``✕ (<label>) `` glyph this suite renders for a marker ``state``.

    The marker keeps its own vocabulary (``auth_failed``) while the bar shows a
    user-facing label (``incorrect_cognee_api_key``), so the state is mapped
    through ``_REASON_LABELS`` first — claude's ``_fail_glyph`` expects the
    label, and codex builds the same string inline.
    """
    label = reason_label(sl, state)
    helper = getattr(sl, "_fail_glyph", None)
    if helper:
        return helper(label)
    return f"✕ ({label}) "


def mode_label(sl, mode: str) -> str:
    """The mode word as the bar prints it for ``mode`` ("local" / "cloud").

    Built from the renderer's own style map rather than by calling
    ``_mode_label()``, which reads the environment itself: a test asserting on the
    whole bar needs the expected label for a *given* mode, not the ambient one.
    codex has no style map — its bar is plain text.
    """
    styles = getattr(sl, "_MODE_STYLES", None)
    if not styles:
        return mode
    return f"{styles[mode]}{mode}\033[0m"


def write_json(path: Path, payload: Any) -> Path:
    """Write a marker file, creating its directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
