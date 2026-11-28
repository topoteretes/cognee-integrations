#!/usr/bin/env python3
"""Lessons while the agent works (coding memory, Claude Code).

``_coding_memory`` gives one lessons block on the first prompt. This module adds
the two lists shown during the session, from the same lessons and the same
ranking (``_coding_memory.select_lessons``):

* ``at_session_start``: up to five lessons of the project with no query, so the
  newest ``test-baseline``, the two newest pitfalls, then the newest lessons.
  Only lessons whose text is already in the lesson cache are used and nothing
  is fetched: with a cold cache there is no list, and the first prompt's block
  covers the session.
* ``for_prompt``: up to five lessons relevant to one prompt. A lesson is
  relevant when the server's lesson recall for the prompt returns it (the
  recall ``build_block`` uses) and it shares a word with the prompt
  (``shares_a_term``): the recall returns its nearest lessons whatever the
  distance and reports no score, so on its own it would list a lesson for
  every prompt.

A lesson is printed in full once per session (the injection ledger remembers
which, and forgets with the context at a compaction or ``/clear``). When it is
relevant again it is one reference line: its first sentence and "shown earlier".

Every function fails quiet: no list, one line in ``hook.log``.
"""

from __future__ import annotations

import re
import time

import _coding_memory
from _plugin_common import hook_log

MAX_LESSONS = 5
#: Seconds SessionStart may spend on its list (one dataset listing, no fetch).
START_BUDGET_SECONDS = 3.0
#: Characters of a lesson's first sentence in a "shown earlier" line.
REFERENCE_CHARS = 100
#: Characters of a lesson's statement in the user-visible session-start message.
MESSAGE_STATEMENT_CHARS = 160
PROMPT_TITLE = "=== Lessons for this prompt (Cognee) ==="
SHOWN_EARLIER = "(shown earlier)"
_UNBOUNDED = 10**9
#: Terms match when equal or when they share this many leading characters.
_SHARED_PREFIX = 5
_TERM_RE = re.compile(r"[A-Za-z][a-z0-9]+|[A-Z0-9]{2,}")
_STOPWORDS = frozenset(
    "the and for with that this from have has had are was were not but you your can how why "
    "what when where which who does did should would could into use used using please make "
    "need want about there their then than also all any get set add fix new run let its out "
    "will just like some more most here very been being they them these those our was now "
    "one two see show tell give take look still only over under after before".split()
)


def terms(text: str) -> set:
    """The content words of ``text``: runs of letters and digits (``snake_case``,
    paths and ``camelCase`` split), lower case, three or more characters, no stopwords."""
    found = {match.group(0).lower() for match in _TERM_RE.finditer(str(text or ""))}
    return {term for term in found if len(term) >= 3 and term not in _STOPWORDS}


def shares_a_term(prompt_terms: set, lesson: dict) -> bool:
    """True when the prompt and the lesson (statement, files, symbols, state key)
    have a content word in common, or two that start with the same five characters."""
    symbols = " ".join(str(s.get("name") or "") for s in lesson.get("symbols") or [])
    files = " ".join(str(f) for f in lesson.get("files") or [])
    lesson_terms = terms(" ".join([str(lesson.get("body") or ""), files, symbols]))
    lesson_terms |= terms(str(lesson.get("state_key") or ""))
    if prompt_terms & lesson_terms:
        return True
    stems = {term[:_SHARED_PREFIX] for term in lesson_terms if len(term) >= _SHARED_PREFIX}
    return any(
        len(term) >= _SHARED_PREFIX and term[:_SHARED_PREFIX] in stems for term in prompt_terms
    )


def ranked(lessons: list, recalled: list) -> list:
    """``select_lessons``' order over every lesson: the one ranking path, with
    its size budgets lifted (the callers cut to ``MAX_LESSONS``)."""
    return _coding_memory.select_lessons(lessons, recalled, budget=_UNBOUNDED, lines_cap=_UNBOUNDED)


def relevant(lessons: list, recalled: list, prompt: str) -> list:
    """The lessons of ``ranked`` that the recall returned and that share a term
    with the prompt, at most ``MAX_LESSONS``."""
    recalled_keys = {
        _coding_memory._match_key(_coding_memory._passage_body(passage)) for passage in recalled
    }
    prompt_terms = terms(prompt)
    chosen = [
        lesson
        for lesson in ranked(lessons, recalled)
        if _coding_memory._match_key(lesson["body"]) in recalled_keys
        and shares_a_term(prompt_terms, lesson)
    ]
    return chosen[:MAX_LESSONS]


def _flat(body: str) -> str:
    return " ".join(str(body or "").split())


def first_sentence(body: str, cap: int = REFERENCE_CHARS) -> str:
    text = _flat(body)
    match = re.search(r"[.!?](?=\s)", text)
    text = text[: match.end()] if match else text
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


def reference_line(lesson: dict) -> str:
    return "- %s %s" % (first_sentence(lesson.get("body")), SHOWN_EARLIER)


def already_shown(lesson: dict, shown: dict) -> bool:
    """Whether the session's context already holds the lesson in full: listed by
    this module (by id), or a line of the first-prompt block (by its opening)."""
    if str(lesson.get("data_id") or "") in shown.get("ids", []):
        return True
    opening = "- " + _flat(lesson.get("body"))[: _coding_memory.RECALL_MATCH_PREFIX]
    return any(line.startswith(opening) for line in shown.get("lines", []))


def _shown(session_key: str) -> dict:
    try:
        from _injection_ledger import lessons_shown

        return lessons_shown(session_key)
    except Exception:
        return {"ids": [], "lines": [], "at_start": False}


def start_list_shown(session_key: str) -> bool:
    return bool(session_key) and _shown(session_key)["at_start"]


def mark_shown(session_key: str, lessons=(), block: str = "", at_start: bool = False) -> None:
    """Remember what was printed in full: listed lessons, or a delivered block's lines."""
    if not session_key:
        return
    try:
        from _injection_ledger import mark_lessons_shown

        mark_lessons_shown(
            session_key,
            ids=[str(lesson.get("data_id") or "") for lesson in lessons if lesson.get("data_id")],
            lines=[line for line in str(block or "").splitlines() if line.startswith("- ")],
            at_start=at_start,
        )
    except Exception as exc:
        hook_log("coding_lessons_mark_failed", {"error": str(exc)[:200]})


def _project_lessons(cwd: str, deadline: float, max_fetches: int | None) -> tuple:
    session_id, dataset = _coding_memory._session()
    if not dataset:
        return "", "", []
    project = _coding_memory.project_scope(dataset, session_id, cwd)
    kwargs = {} if max_fetches is None else {"max_fetches": max_fetches}
    dataset_id, lessons = _coding_memory.list_lessons(dataset, project, deadline, **kwargs)
    return dataset, dataset_id, lessons


def _line_for(cwd: str, lessons: list, deadline: float):
    """``(line_for, root)``: lines with their git tags, the stamped symbols of
    ``lessons`` compared with the code graph first (one bounded call), as the block does."""
    root = _coding_memory.git_root(cwd)
    in_git = bool(root)
    root = root or cwd
    stamps = _coding_memory._stamps()
    if in_git and lessons and stamps is not None:
        stamps.mark_changed(lessons, root, _coding_memory._remaining(deadline))
    return _coding_memory._line_maker(root, in_git)


def at_session_start(session_key: str, cwd: str, budget: float = START_BUDGET_SECONDS) -> dict:
    """``{"context", "message", "shown", "total"}`` for SessionStart, or {} when
    there is nothing to list (no lessons, a cold cache, a server that did not answer).

    ``context`` is for the model, ``message`` the compact list for the user.
    """
    if not _coding_memory.server_supported():
        return {}
    deadline = time.monotonic() + budget
    try:
        dataset, _, lessons = _project_lessons(cwd, deadline, 0)
        if not lessons:
            hook_log("coding_start_lessons", {"shown": 0, "cached": 0})
            return {}
        # No query: after the baseline and the pitfalls, the newest lessons.
        chosen = ranked(lessons, [lesson["body"] for lesson in lessons])[:MAX_LESSONS]
        line_for = _line_for(cwd, chosen, deadline)
        lines = _coding_memory._cap_lines(
            [line_for(lesson) for lesson in chosen], _coding_memory.LESSON_LINES_CAP
        )
        chosen = chosen[: len(lines)]
        if not lines:
            return {}
        head = (
            "Cognee lessons for this project (%d of %d, from earlier sessions in the %s dataset)"
            % (
                len(lines),
                len(lessons),
                dataset,
            )
        )
        short = []
        for lesson, line in zip(chosen, lines):
            body = _flat(lesson.get("body"))
            tag = line[len("- " + body) :].strip()
            if len(body) > MESSAGE_STATEMENT_CHARS:
                body = body[: MESSAGE_STATEMENT_CHARS - 1].rstrip() + "…"
            short.append(("- %s %s" % (body, tag)).rstrip())
        mark_shown(session_key, chosen, at_start=True)
        hook_log("coding_start_lessons", {"shown": len(lines), "cached": len(lessons)})
        return {
            "context": "\n".join([head + ". Check them before exploring:", *lines]),
            "message": "\n".join([head + ":", *short]),
            "shown": len(lines),
            "total": len(lessons),
        }
    except Exception as exc:
        hook_log("coding_start_lessons_failed", {"error": str(exc)[:200]})
        return {}


def for_prompt(session_key: str, prompt: str, cwd: str, deadline: float) -> list:
    """The prompt's lessons as ``{"lesson", "line", "full"}``, most relevant first.

    ``full`` lines carry the statement and its markers; a lesson the session
    already holds is a reference line. [] when no lesson is relevant.
    """
    if not prompt.strip() or not _coding_memory.server_supported():
        return []
    deadline = min(deadline, time.monotonic() + _coding_memory.DELIVERY_BUDGET_SECONDS)
    try:
        dataset, dataset_id, lessons = _project_lessons(cwd, deadline, None)
        if not lessons:
            return []
        recalled = _coding_memory.recall_passages(prompt, dataset, dataset_id, deadline)
        chosen = relevant(lessons, recalled, prompt)
        shown = _shown(session_key)
        new = [lesson for lesson in chosen if not already_shown(lesson, shown)]
        line_for = _line_for(cwd, new, deadline) if new else None
        return [
            {"lesson": lesson, "line": line_for(lesson), "full": True}
            if any(lesson is item for item in new)
            else {"lesson": lesson, "line": reference_line(lesson), "full": False}
            for lesson in chosen
        ]
    except Exception as exc:
        hook_log("coding_prompt_lessons_failed", {"error": str(exc)[:200]})
        return []


def is_lesson_passage(passage: str) -> bool:
    """A recalled passage that is a lesson document (its ``Lesson-Type`` header,
    below an optional mark and ``source:`` lines): lessons reach the prompt
    through the list, not the recall section."""
    return any(
        line.strip().startswith("Lesson-Type:") for line in str(passage or "").splitlines()[:12]
    )
