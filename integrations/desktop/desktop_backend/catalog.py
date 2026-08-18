"""Catalog of indexed files, plus launcher-style filename matching.

The catalog is the backend's own source of truth for "which files exist and
where" -- semantic results coming back from cognee are chunks of text, and the
catalog is what maps them back to an openable path (by basename). It also
powers the instant filename results the panel shows while typing, which work
even before cognify has finished (like any launcher showing name hits
first).

Persisted as one JSON file so state survives backend restarts; no database
needed for the tens of thousands of entries a personal index holds.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional


class Catalog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        # path -> {"name", "mtime", "size"}
        self._entries: dict[str, dict[str, Any]] = {}
        self._roots: list[str] = []
        self._load()

    # -- persistence ---------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text())
            self._entries = data.get("entries", {})
            self._roots = data.get("roots", [])
        except (OSError, ValueError):
            self._entries, self._roots = {}, []

    def save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"entries": self._entries, "roots": self._roots}))
            tmp.replace(self._path)

    # -- entries ---------------------------------------------------------
    def upsert(self, path: str, mtime: float, size: int) -> None:
        with self._lock:
            self._entries[path] = {"name": Path(path).name, "mtime": mtime, "size": size}

    def needs_index(self, path: str, mtime: float) -> bool:
        entry = self._entries.get(path)
        return entry is None or mtime > entry["mtime"]

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self, limit: int = 500) -> list[dict[str, Any]]:
        """Indexed files, newest first — the receipts behind "N files indexed"."""
        with self._lock:
            items = sorted(
                self._entries.items(), key=lambda kv: kv[1].get("mtime", 0), reverse=True
            )
        return [
            {
                "path": path,
                "name": entry.get("name", ""),
                "mtime": entry.get("mtime", 0),
                "size": entry.get("size", 0),
            }
            for path, entry in items[:limit]
        ]

    @property
    def roots(self) -> list[str]:
        return list(self._roots)

    def add_roots(self, roots: list[str]) -> None:
        with self._lock:
            for root in roots:
                if root not in self._roots:
                    self._roots.append(root)

    # -- lookups -----------------------------------------------------------
    def find_by_basename(self, basename: str) -> Optional[str]:
        """Map a document name cognee reports back to a full catalog path."""
        needle = basename.lower()
        for path, entry in self._entries.items():
            name = entry["name"].lower()
            if name == needle or Path(name).stem == needle:
                return path
        return None

    def match_names(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Rank catalog entries against ``query`` the way launchers rank names:
        exact > prefix > word-start > substring > in-order subsequence, with the
        more recently modified file winning ties."""
        q = query.strip().lower()
        if not q:
            return []
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for path, entry in self._entries.items():
            score = _name_score(entry["name"].lower(), q)
            if score > 0:
                scored.append((score + min(entry["mtime"] / 1e12, 0.99), path, entry))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [
            {"path": path, "name": entry["name"], "score": round(score, 3)}
            for score, path, entry in scored[:limit]
        ]


def _name_score(name: str, q: str) -> float:
    stem = name.rsplit(".", 1)[0]
    if stem == q or name == q:
        return 100.0
    if name.startswith(q):
        return 80.0
    # word-start match: "proj plan" hits "project-plan.md"
    words = [w for w in _split_words(stem) if w]
    parts = q.split()
    if parts and all(any(w.startswith(p) for w in words) for p in parts):
        return 70.0
    if q in name:
        return 60.0
    if _is_subsequence(q.replace(" ", ""), name):
        return 40.0
    return 0.0


def _split_words(s: str) -> list[str]:
    out, cur = [], ""
    for ch in s:
        if ch.isalnum():
            cur += ch
        else:
            out.append(cur)
            cur = ""
    out.append(cur)
    return out


def _is_subsequence(needle: str, hay: str) -> bool:
    it = iter(hay)
    return all(ch in it for ch in needle)
