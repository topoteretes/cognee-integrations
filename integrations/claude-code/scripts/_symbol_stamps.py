#!/usr/bin/env python3
"""Symbol stamps for coding memory: which symbols a turn changed.

Used by ``_coding_memory`` (which imports this module only where the plugin
ships it) in two places, both through the server's ``locate`` CODE operation
over the checkout's path-indexed code graph:

* **At the end of a turn** (``stamp_turns``): the code graph is brought up to
  the working tree, the line ranges the turn's edit calls changed are mapped to
  symbols, and the turn's ``context`` carries them
  as ``symbols`` (repo, file, name, kind, body_hash). The server stores them on
  the turn's lessons.
* **In the first-prompt lessons block** (``mark_changed``): one ``locate`` call
  with the stamps of the lessons shown. A lesson is marked for a stamped symbol
  whose body hash differs now, or that no longer exists.

Line ranges come from the edit tools' own inputs (``Edit`` / ``MultiEdit`` /
``Write``), not from a git diff: the range is where the edit's new text sits in
the file when the turn ends. An edit whose text a later edit replaced, a
deletion (empty new text) and a change made through the shell yield no range.

Every failure is quiet: the turn is sent without stamps, the lesson keeps its
file-level marker, and one line goes to ``hook.log``.
"""

from __future__ import annotations

import os

import _code_analysis
import _code_graph
from _plugin_common import hook_log

#: Seconds one ``locate`` call may take inside a hook.
LOCATE_TIMEOUT_SECONDS = 4.0
#: Seconds one capture may spend on stamps in all: the graph refresh, then the
#: ``locate`` calls. The Stop hook is asynchronous with a 60 s timeout.
STAMP_BUDGET_SECONDS = 20.0
#: Stamps kept per turn: the context is one JSON string on the QA entry.
MAX_STAMPS_PER_TURN = 200
_STAMP_KEYS = ("repo", "file", "name", "kind")


def _runtime() -> dict:
    from _plugin_common import _api_key, _local_api_url

    return {"service_url": _local_api_url(), "api_key": _api_key()}


def _line_ranges(text: str, needle: str, every: bool) -> list:
    """1-based ``(start, end)`` line ranges of ``needle`` in ``text``."""
    ranges, offset = [], 0
    while needle.strip():
        index = text.find(needle, offset)
        if index < 0:
            break
        start = text.count("\n", 0, index) + 1
        end = start + needle.rstrip("\n").count("\n")
        ranges.append((start, end))
        if not every:
            break
        offset = index + len(needle)
    return ranges


def edit_locations(calls: list, root: str) -> list:
    """``{"file", "start", "end"}`` for what the edit calls changed under ``root``."""
    real_root = os.path.realpath(root)
    texts: dict = {}
    seen, out = set(), []

    def read(path: str):
        if path not in texts:
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    texts[path] = handle.read()
            except OSError:
                texts[path] = None
        return texts[path]

    for call in calls or []:
        tool_input = call.get("input") if isinstance(call.get("input"), dict) else {}
        name = str(call.get("name") or "")
        if call.get("is_error") or name not in ("Edit", "MultiEdit", "Write"):
            continue
        path = str(tool_input.get("file_path") or "")
        if not path:
            continue
        path = os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))
        if path != real_root and not path.startswith(real_root.rstrip(os.sep) + os.sep):
            continue
        text = read(path)
        if text is None:
            continue
        if name == "Write":
            ranges = [(1, max(1, text.count("\n") + (0 if text.endswith("\n") else 1)))]
        else:
            edits = tool_input.get("edits") if name == "MultiEdit" else [tool_input]
            ranges = []
            for edit in edits if isinstance(edits, list) else []:
                if isinstance(edit, dict):
                    ranges.extend(
                        _line_ranges(
                            text, str(edit.get("new_string") or ""), bool(edit.get("replace_all"))
                        )
                    )
        for start, end in ranges:
            if (path, start, end) not in seen:
                seen.add((path, start, end))
                out.append({"file": path, "start": start, "end": end})
    return out


def _locate(root: str, query: dict, timeout: float, opener=None):
    """The ``locate`` result for the code graph of ``root``, or None with a reason."""
    state = _code_graph.find_indexed_repo(root)
    if state.get("spec_kind") != "path" or not state.get("dataset"):
        return None, "not_indexed_by_path"
    rt = _runtime()
    if not _code_analysis.operation_known(rt, "locate"):
        return None, "unknown_operation"
    from _deadlines import bounded_call

    try:
        answer = bounded_call(
            lambda: _code_analysis.code_operation(
                rt,
                state["dataset"],
                {"operation": "locate", **query},
                timeout=timeout,
                opener=opener,
            ),
            timeout,
        )
    except TimeoutError:
        return None, "timeout"
    if not answer["ok"]:
        return None, str(answer["code"])
    if not isinstance(answer["raw"].get("symbols"), list):
        return None, "malformed"
    return answer["raw"], ""


def _graph_current(root: str, timeout: float, opener=None) -> bool:
    """Make the code graph hold the working tree before it is asked about it.

    The line ranges and body hashes are the file's as the turn left it, and
    coding memory runs no per-turn re-index of its own: when the tree changed
    since the last index, the repository is re-submitted and the call waits for
    the load. False when the graph could not be brought up to date.
    """
    rt = _runtime()
    kwargs = {"run_in_background": False, "timeout": timeout}
    if opener is not None:
        kwargs["opener"] = opener
    outcome = _code_graph.reingest_if_changed(root, rt["service_url"], rt["api_key"], **kwargs)
    return outcome.get("changed") is False or bool(outcome.get("submitted"))


def stamp_turns(groups: dict, root: str, budget: float = STAMP_BUDGET_SECONDS, opener=None) -> dict:
    """``{turn id: stamps}`` for ``{turn id: calls}``; turns without stamps are left out."""
    import time

    try:
        deadline = time.monotonic() + budget
        edited = {key: calls for key, calls in groups.items() if edit_locations(calls, root)}
        if not edited:
            return {}
        state = _code_graph.find_indexed_repo(root)
        if state.get("spec_kind") != "path" or not _code_analysis.operation_known(
            _runtime(), "locate"
        ):
            hook_log("symbol_stamps_skipped", {"reason": "no_graph_or_operation"})
            return {}
        if not _graph_current(str(state["repo_root"]), budget - LOCATE_TIMEOUT_SECONDS, opener):
            hook_log("symbol_stamps_skipped", {"reason": "graph_not_current"})
            return {}
        out = {}
        for key, calls in edited.items():
            left = deadline - time.monotonic()
            if left < 0.5:
                hook_log("symbol_stamps_skipped", {"reason": "deadline", "turns": len(edited)})
                break
            symbols = turn_symbols(calls, root, min(left, LOCATE_TIMEOUT_SECONDS), opener)
            if symbols:
                out[key] = symbols
        return out
    except Exception as exc:
        hook_log("symbol_stamps_skipped", {"reason": "error", "error": str(exc)[:200]})
        return {}


def _well_formed(symbol) -> bool:
    if not isinstance(symbol, dict):
        return False
    if not all(isinstance(symbol.get(k), str) and symbol[k] for k in _STAMP_KEYS):
        return False
    return symbol.get("body_hash") is None or isinstance(symbol["body_hash"], str)


def turn_symbols(calls: list, root: str, timeout: float = LOCATE_TIMEOUT_SECONDS, opener=None):
    """The stamps of the symbols the turn's edit calls changed; [] on any failure."""
    try:
        locations = edit_locations(calls, root)
        if not locations:
            return []
        raw, reason = _locate(root, {"locations": locations}, timeout, opener)
        if raw is None:
            hook_log("symbol_stamps_skipped", {"reason": reason, "locations": len(locations)})
            return []
        # The server drops a whole list over one malformed entry: send clean ones only.
        symbols = [
            {**{k: s[k] for k in _STAMP_KEYS}, "body_hash": s.get("body_hash")}
            for s in raw["symbols"]
            if _well_formed(s)
        ]
        return symbols[:MAX_STAMPS_PER_TURN]
    except Exception as exc:
        hook_log("symbol_stamps_skipped", {"reason": "error", "error": str(exc)[:200]})
        return []


def _short(name: str) -> str:
    """``inventory/store.InventoryStore.total`` as ``InventoryStore.total``."""
    tail = name.rsplit("/", 1)[-1]
    return tail.split(".", 1)[1] if "." in tail else tail


def mark_changed(lessons: list, root: str, timeout: float = LOCATE_TIMEOUT_SECONDS, opener=None):
    """Set ``symbol_state`` (``{"changed": [...], "gone": [...]}``, short names) on
    every lesson that has stamps. True when the stamps were compared; on any
    failure nothing is set and False is returned."""
    try:
        stamped = [
            (lesson, [s for s in lesson.get("symbols") or [] if _well_formed(s)])
            for lesson in lessons
        ]
        stamped = [(lesson, stamps) for lesson, stamps in stamped if stamps]
        if not stamped:
            return False
        wanted: dict = {}
        for _, stamps in stamped:
            for stamp in stamps:
                wanted[tuple(stamp[k] for k in _STAMP_KEYS)] = {k: stamp[k] for k in _STAMP_KEYS}
        raw, reason = _locate(root, {"symbols": list(wanted.values())}, timeout, opener)
        if raw is None:
            hook_log("symbol_marks_skipped", {"reason": reason, "stamps": len(wanted)})
            return False
        current = {
            tuple(s[k] for k in _STAMP_KEYS): s.get("body_hash")
            for s in raw["symbols"]
            if _well_formed(s)
        }
        if not current:
            # A graph not mapped for locate yet reports every stamp missing:
            # that is "cannot tell", not "every symbol is gone".
            hook_log("symbol_marks_skipped", {"reason": "nothing_matched", "stamps": len(wanted)})
            return False
        for lesson, stamps in stamped:
            changed, gone = [], []
            for stamp in stamps:
                key = tuple(stamp[k] for k in _STAMP_KEYS)
                if key not in current:
                    gone.append(_short(stamp["name"]))
                # A null hash on either side cannot tell: not marked.
                elif stamp.get("body_hash") and current[key] and current[key] != stamp["body_hash"]:
                    changed.append(_short(stamp["name"]))
            lesson["symbol_state"] = {
                "changed": list(dict.fromkeys(changed)),
                "gone": list(dict.fromkeys(gone)),
            }
        return True
    except Exception as exc:
        hook_log("symbol_marks_skipped", {"reason": "error", "error": str(exc)[:200]})
        return False
