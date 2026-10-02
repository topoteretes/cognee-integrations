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
        # root -> [".pdf", ".docx"] — only these types index under that root
        self._root_filters: dict[str, list[str]] = {}
        # root -> "personal" | "work" — memory scoping label
        self._root_labels: dict[str, str] = {}
        # explicitly forgotten paths: a re-sync of a still-watched folder
        # must not resurrect them
        self._ignored: set[str] = set()
        # roots with live sync switched off: stay searchable, stop updating
        self._paused: set[str] = set()
        self._load()

    # -- persistence ---------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text())
            self._entries = data.get("entries", {})
            self._roots = data.get("roots", [])
            self._root_filters = data.get("root_filters", {})
            self._root_labels = data.get("root_labels", {})
            self._ignored = set(data.get("ignored", []))
            self._paused = set(data.get("paused", []))
        except (OSError, ValueError):
            self._entries, self._roots, self._root_filters = {}, [], {}
            self._root_labels, self._ignored, self._paused = {}, set(), set()

    def save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "entries": self._entries,
                        "roots": self._roots,
                        "root_filters": self._root_filters,
                        "root_labels": self._root_labels,
                        "ignored": sorted(self._ignored),
                        "paused": sorted(self._paused),
                    }
                )
            )
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

    def remove(self, path: str) -> bool:
        """Drop one file from the catalog. True if it was there."""
        with self._lock:
            return self._entries.pop(path, None) is not None

    def remove_root(self, root: str) -> int:
        """Drop a watched root and every catalog entry beneath it.

        Returns how many file entries went with it."""
        prefix = root.rstrip("/") + "/"
        with self._lock:
            doomed = [p for p in self._entries if p.startswith(prefix) or p == root]
            for p in doomed:
                del self._entries[p]
            self._roots = [r for r in self._roots if r != root]
            self._root_filters.pop(root, None)
            self._root_labels.pop(root, None)
            self._paused.discard(root)
            # a re-added root starts fresh: old tombstones go with it
            self._ignored = {p for p in self._ignored if not (p == root or p.startswith(prefix))}
        return len(doomed)

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

    def ignore(self, path: str) -> None:
        """Tombstone: keep this path out of every future re-sync."""
        with self._lock:
            self._ignored.add(path)

    def is_ignored(self, path: str) -> bool:
        return path in self._ignored

    def unignore_exact(self, paths: list[str]) -> None:
        """Clear tombstones for exactly these paths (not their contents)."""
        with self._lock:
            self._ignored -= set(paths)

    def set_paused(self, root: str, paused: bool) -> None:
        with self._lock:
            if paused:
                self._paused.add(root)
            else:
                self._paused.discard(root)

    @property
    def paused_roots(self) -> list[str]:
        with self._lock:
            return sorted(self._paused)

    @property
    def root_labels(self) -> dict[str, str]:
        with self._lock:
            return dict(self._root_labels)

    @property
    def root_filters(self) -> dict[str, list[str]]:
        with self._lock:
            return dict(self._root_filters)

    @property
    def roots(self) -> list[str]:
        return list(self._roots)

    def add_roots(
        self,
        roots: list[str],
        extensions: list[str] | None = None,
        label: str = "",
    ) -> None:
        """Register roots; ``extensions`` (e.g. [".pdf", ".docx"]) restricts
        what indexes under these roots, now and on every future re-sync;
        ``label`` tags everything under them (personal / work)."""
        cleaned = [e.strip().lstrip("*").lower() for e in (extensions or [])]
        normalized = [e if e.startswith(".") else f".{e}" for e in cleaned if e.strip(".")]
        with self._lock:
            for root in roots:
                if root not in self._roots:
                    self._roots.append(root)
                if normalized:
                    self._root_filters[root] = normalized
                if label:
                    self._root_labels[root] = label
        # picking a path explicitly is consent to index THAT path again —
        # but only that exact path: re-indexing a parent folder must never
        # resurrect files the user forgot individually
        self.unignore_exact(roots)

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
