"""Session-scoped, atomic record of the entries already in Claude's context."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from _file_lock import file_lock
from _injection_budget import fit_sections


def entry_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]


def session_dir(session_id: str) -> Path:
    if (
        not session_id
        or session_id in {".", ".."}
        or not all(c.isalnum() or c in "._-" for c in session_id)
    ):
        raise ValueError("unsafe session id")
    return Path.home() / ".cognee-plugin" / "claude-code" / "sessions" / session_id


# On Windows a file another process has open (a reader, the indexer, an
# antivirus scan of a file just written) cannot be replaced: os.replace raises
# PermissionError for the few milliseconds the handle is held. Tried again with
# a short, growing wait before the write is given up.
_REPLACE_ATTEMPTS = 8
_REPLACE_WAIT_SECONDS = 0.01
_RETRY_REPLACE = os.name == "nt"


def _replace(temporary: str, path: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if not _RETRY_REPLACE or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_WAIT_SECONDS * (attempt + 1))


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + "-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def reset(session_id: str) -> None:
    directory = session_dir(session_id)
    with file_lock(directory / "injected.lock", timeout=0.2) as acquired:
        if not acquired:
            raise OSError("injection ledger is busy")
        atomic_json(directory / "injected.json", {"entry_hashes": [], "guidance_hashes": []})


def seed(session_id: str, hashes: list[str], guidance_hashes: list[str] = None) -> None:
    directory = session_dir(session_id)
    with file_lock(directory / "injected.lock", timeout=0.2) as acquired:
        if not acquired:
            raise OSError("injection ledger is busy")
        data = _read(directory / "injected.json")
        data["entry_hashes"] = sorted(set(data.get("entry_hashes", [])) | set(hashes))
        if guidance_hashes:
            data["guidance_hashes"] = sorted(set(guidance_hashes))
        atomic_json(directory / "injected.json", data)


def is_guidance_section(section: dict) -> bool:
    """Active agent guidance, deduplicated by its whole current set rather than per entry."""
    return section.get("source") == "session_context"


def render(
    session_id: str, header: str, sections: list[dict], budget: int, measure=None
) -> tuple[str, dict, str]:
    """Filter before fitting; record only entries actually retained in the output."""
    directory = session_dir(session_id)
    with file_lock(directory / "injected.lock", timeout=0.2) as acquired:
        if not acquired:
            raise OSError("injection ledger is busy")
        path = directory / "injected.json"
        data = _read(path)
        seen = set(data.get("entry_hashes", []))
        guidance = set(data.get("guidance_hashes", []))
        filtered, duplicate_count, candidates = [], 0, 0
        new_guidance = set()
        # Guidance hashes this response saw that are already in context: with the
        # new ones kept below, they are the full guidance set Claude now holds.
        present_guidance = set()
        deduped_entries = []
        for section in sections:
            entries = []
            is_guidance = is_guidance_section(section)
            for entry in section.get("entries", []):
                digest = entry_hash(entry)
                # ``dedupe: False`` sections (the credit instruction) go out every time.
                if section.get("dedupe", True) and digest in (guidance if is_guidance else seen):
                    if is_guidance:
                        present_guidance.add(digest)
                    duplicate_count += int(section.get("counted", True))
                    deduped_entries.append(entry)
                    continue
                entries.append(entry)
                candidates += int(section.get("counted", True))
                # Avoid duplicates returned by different datasets in this response.
                seen.add(digest)
                if is_guidance:
                    new_guidance.add(digest)
            filtered.append({**section, "entries": entries})
        decorated = f"{header}; {candidates} new, {duplicate_count} already in context"
        text, stats = fit_sections(
            [{"entries": [decorated], "required": True}] + filtered, budget, measure=measure
        )
        actual_header = (
            f"{header}; {stats['entries_kept']} new, {duplicate_count} already in context"
        )
        text = actual_header + text[len(decorated) :]
        # Compare the bounded payload with deduplication disabled, not the raw
        # retrieved text. Budget-dropped entries and replacement context must not
        # inflate savings. This is context-size reduction, not billed token usage.
        saved_chars = 0
        if deduped_entries:
            baseline, _ = fit_sections(
                [{"entries": [header], "required": True}] + sections, budget, measure=measure
            )
            saved_chars = max(0, len(baseline) - len(text))
        kept = {entry_hash(entry) for entry in stats["entries"]}
        data["entry_hashes"] = sorted(set(data.get("entry_hashes", [])) | kept)
        if new_guidance & kept:
            # Record all guidance now in context, not just the changed part:
            # otherwise unchanged entries look new on every later prompt.
            data["guidance_hashes"] = sorted(present_guidance | (new_guidance & kept))
        atomic_json(path, data)
        stats.update(
            entries_deduped=duplicate_count,
            dedup_chars_saved=saved_chars,
            context_chars=len(text),
            deduped_entries=deduped_entries,
        )
        return text, stats, actual_header
