#!/usr/bin/env python3
"""Trim an ``only_context`` recall item down to the retrieved context (SDK-904).

On cognee >= 1.6.0 a completion search with ``only_context=true`` returns the
whole prompt its own LLM would have received: the conversation history, the
question rendered through the retriever's template, the retrieved context, and
the session guidance block. The host agent already holds the conversation and
needs no answering instructions, so injecting that string whole re-sends what
is already in its context window — about 60% of a mid-session payload was the
agent's own previous turns. This module keeps only the retrieved context and
shrinks it deterministically, without an LLM and without touching what the
server retrieved.

Two anchors, both ours to know:

* the question marker ``The question is: `<prompt>```, rendered from the very
  prompt the hook sent, so it matches exactly once however many backticks the
  prompt or the context carry; the context slot opens at the first backtick
  after it, in every one of the server's question templates;
* the end: the final character when the text ends with a backtick, otherwise
  the last ``\\n\\n## Active session guidance`` (the guidance block is the only
  layer the server renders after the template).

Anything that does not fit is left untouched and reported as unparsed: a
template change can only cost tokens, never memory.

Stdlib only, no plugin imports: this file is copied verbatim between the hook
suites like ``_recall_http.py``.
"""

from __future__ import annotations

import json
import os
import re

#: Every cognee question template starts with this line.
QUESTION_PREFIX = "The question is: `"
#: Title of the server-rendered guidance block (session_context_builder.BLOCK_TITLE).
GUIDANCE_TITLE = "## Active session guidance"

PASSAGES_HEADING = "## Relevant passages"
ENTITIES_HEADING = "## Relevant entities"
FACTS_HEADING = "## Related facts"
#: The hybrid retriever joins passages with this, no blank lines around it;
#: a markdown rule inside an answer has them, which ``split_passages`` uses.
PASSAGE_SEPARATOR = "\n---\n"

ENV_PASSAGE_CHARS = "COGNEE_RECALL_PASSAGE_CHARS"
ENV_CONTEXT_CHARS = "COGNEE_RECALL_CONTEXT_CHARS"
DEFAULT_PASSAGE_CHARS = 2000
DEFAULT_CONTEXT_CHARS = 12000

_SECTION_RE = re.compile(
    r"(?m)^(## Relevant passages|## Relevant entities|## Related facts)[ \t]*\n"
)
#: The header a bridged session chunk opens with. ``Session ID:`` is the
#: transcript document; ``# Session learning … (session <id>)`` the distilled note.
_SESSION_ID_RE = re.compile(r"^Session ID:\s*(\S+)", re.M)
_SESSION_LEARNING_RE = re.compile(r"^# Session learning\b[^\n]*?\(session ([^)\s]+)\)", re.M)
_DOC_HEADER_RE = re.compile(r"^(Session ID:|# Session learning\b)")
#: Entity bullets the graph builder emits for every node: a bare type statement
#: (``X is a concept.``) and node-set membership (``X belongs to set …``).
_TYPE_BULLET_RE = re.compile(r"^- .+? is an? [a-z]+(?:[ _-][a-z]+)?\.$")
_SET_BULLET_RE = re.compile(r"^- .+ belongs to set \S+\.?$")


def _int_env(name: str, default: int) -> int:
    """Non-negative int from the environment; ``0`` disables, junk means default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def passage_chars_limit() -> int:
    return _int_env(ENV_PASSAGE_CHARS, DEFAULT_PASSAGE_CHARS)


def context_chars_limit() -> int:
    return _int_env(ENV_CONTEXT_CHARS, DEFAULT_CONTEXT_CHARS)


# ---------------------------------------------------------------------------
# Step 2: cut the template away
# ---------------------------------------------------------------------------


def split_only_context(text: str, prompt: str) -> dict:
    """Split a 1.6.0 ``only_context`` item into its layers.

    Returns ``{"history", "context", "guidance", "parsed"}``. When the text
    cannot be read as the server's prompt (an older server's bare context, a
    rewritten question, a template this module does not know) ``parsed`` is
    False and the whole text sits in ``context`` — fail-open.
    """
    text = str(text or "")
    unparsed = {"history": "", "context": text, "guidance": "", "parsed": False}
    marker = f"{QUESTION_PREFIX}{prompt}`"
    question_at = text.find(marker)
    if question_at < 0:
        return unparsed
    start = text.find("`", question_at + len(marker))
    if start < 0:
        return unparsed
    start += 1
    if text.endswith("`"):
        end = len(text) - 1
        guidance = ""
    else:
        end = text.rfind("`\n\n" + GUIDANCE_TITLE)
        if end < start:
            return unparsed
        guidance = text[end + 1 :].strip()
    return {
        "history": text[:question_at].rstrip(),
        "context": text[start:end],
        "guidance": guidance,
        "parsed": True,
    }


# ---------------------------------------------------------------------------
# Step 3: shrink the context
# ---------------------------------------------------------------------------


def split_sections(context: str) -> dict:
    """``{"preamble", "passages", "entities", "facts"}`` — a key is None when absent.

    ``preamble`` is whatever precedes the first heading (the hybrid retriever's
    optional global-context section, or an unsectioned context from another
    retriever), returned as-is.
    """
    parts = _SECTION_RE.split(context or "")
    sections = {"preamble": parts[0], "passages": None, "entities": None, "facts": None}
    names = {PASSAGES_HEADING: "passages", ENTITIES_HEADING: "entities", FACTS_HEADING: "facts"}
    for i in range(1, len(parts) - 1, 2):
        sections[names[parts[i]]] = parts[i + 1]
    return sections


def split_passages(block: str) -> list[str]:
    """Passages of a ``## Relevant passages`` block.

    The server joins passages with exactly ``\\n---\\n`` — the next chunk's text
    starts right after the newline. A markdown horizontal rule inside an
    assistant answer is the same three characters, but it sits between blank
    lines (``\\n\\n---\\n\\n``), so after the split that piece starts with a
    newline. Such a piece is glued back onto the one before it, unless it
    opens with a document header. Documents without a header (remembered text,
    blog posts) start with their own text and stay separate.
    """
    pieces = (block or "").strip("\n").split(PASSAGE_SEPARATOR)
    passages: list[str] = []
    for piece in pieces:
        continuation = piece.startswith("\n") and not _DOC_HEADER_RE.match(piece.lstrip())
        if passages and continuation:
            passages[-1] = passages[-1] + PASSAGE_SEPARATOR + piece
        else:
            passages.append(piece)
    return [p for p in passages if p.strip()]


def passage_session_id(passage: str) -> str:
    """The session a bridged chunk came from, or "" for any other document."""
    head = passage.lstrip()[:400]
    match = _SESSION_ID_RE.search(head) or _SESSION_LEARNING_RE.search(head)
    return match.group(1) if match else ""


def cap_passage(passage: str, limit: int) -> str:
    """``passage`` cut to about ``limit`` chars at a paragraph break, marked."""
    if limit <= 0 or len(passage) <= limit:
        return passage
    cut = passage.rfind("\n\n", 0, limit)
    if cut < limit // 2:
        cut = passage.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = limit
    head = passage[:cut].rstrip()
    return f"{head}\n…[+{len(passage) - len(head)} chars]"


def prune_entities(block: str) -> tuple[str, int]:
    """Drop type and node-set bullets, then headings left without bullets.

    Returns ``(text, removed_lines)``; ``text`` is "" when nothing with content
    survives.
    """
    lines = (block or "").strip("\n").split("\n")
    kept: list[str] = []
    removed = 0
    for line in lines:
        if _TYPE_BULLET_RE.match(line) or _SET_BULLET_RE.match(line):
            removed += 1
            continue
        kept.append(line)
    pruned: list[str] = []
    for i, line in enumerate(kept):
        if line.startswith("### "):
            nxt = kept[i + 1] if i + 1 < len(kept) else ""
            if not nxt.startswith("- "):
                removed += 1
                continue
        pruned.append(line)
    text = "\n".join(pruned).strip("\n")
    if not any(line.startswith("- ") for line in pruned):
        return "", removed
    return text, removed


def minimize_context(
    context: str,
    session_id: str = "",
    *,
    passage_chars: int | None = None,
    context_chars: int | None = None,
) -> tuple[str, dict]:
    """Shrink a parsed context. Returns ``(text, stats)``.

    In order: drop the passages that are this session's own bridged turns, cap
    each remaining passage, prune the entity bullets that carry nothing, keep
    the facts, and as a last resort trim passages from the end until the whole
    thing fits ``context_chars``. Entities and facts are never trimmed by the
    budget. An unsectioned context (no headings) passes through untouched.
    """
    if passage_chars is None:
        passage_chars = passage_chars_limit()
    if context_chars is None:
        context_chars = context_chars_limit()
    stats = {
        "passages_in": 0,
        "passages_dropped": 0,
        "passages_capped": 0,
        "entity_lines_removed": 0,
        "budget_trimmed": 0,
    }
    sections = split_sections(context)
    if sections["passages"] is None and sections["entities"] is None and sections["facts"] is None:
        return (context or "").strip(), stats

    out: list[str] = []
    preamble = (sections["preamble"] or "").strip()
    if preamble:
        out.append(preamble)

    passages: list[str] = []
    if sections["passages"] is not None:
        raw = split_passages(sections["passages"])
        stats["passages_in"] = len(raw)
        for passage in raw:
            if session_id and passage_session_id(passage) == session_id:
                stats["passages_dropped"] += 1
                continue
            capped = cap_passage(passage, passage_chars)
            if capped is not passage:
                stats["passages_capped"] += 1
            passages.append(capped)

    entities = ""
    if sections["entities"] is not None:
        entities, removed = prune_entities(sections["entities"])
        stats["entity_lines_removed"] = removed

    facts = (sections["facts"] or "").strip("\n") if sections["facts"] is not None else ""

    def render(kept_passages: list[str]) -> str:
        blocks = list(out)
        if kept_passages:
            blocks.append(PASSAGES_HEADING + "\n" + PASSAGE_SEPARATOR.join(kept_passages))
        if entities:
            blocks.append(ENTITIES_HEADING + "\n" + entities)
        if facts.strip():
            blocks.append(FACTS_HEADING + "\n" + facts)
        return "\n\n".join(blocks).strip()

    text = render(passages)
    while context_chars > 0 and len(text) > context_chars and passages:
        passages.pop()
        stats["budget_trimmed"] += 1
        text = render(passages)
    return text, stats


def trim_recall_text(
    text: str,
    prompt: str,
    session_id: str = "",
    *,
    passage_chars: int | None = None,
    context_chars: int | None = None,
) -> tuple[str, str, dict]:
    """Steps 2 and 3 together. Returns ``(context, guidance, stats)``.

    ``context`` is what to inject under the memory heading; ``guidance`` the
    server's trailing guidance block when one came with the item (empty when
    the request carried no session id and no durable preferences exist).
    ``stats`` carries ``parsed``, ``chars_in``, ``chars_out``, ``history_chars``
    and the minimizer's counters. Unparsed text is returned whole.
    """
    text = str(text or "")
    layers = split_only_context(text, prompt)
    stats = {
        "parsed": layers["parsed"],
        "chars_in": len(text),
        "history_chars": len(layers["history"]),
    }
    if not layers["parsed"]:
        stats["chars_out"] = len(text)
        return text, "", stats
    context, more = minimize_context(
        layers["context"],
        session_id,
        passage_chars=passage_chars,
        context_chars=context_chars,
    )
    stats.update(more)
    stats["chars_out"] = len(context)
    return context, layers["guidance"], stats


# ---------------------------------------------------------------------------
# Code lane rendering
# ---------------------------------------------------------------------------


def code_facts(entry_text: str) -> list | None:
    """The ``facts`` list of a code-lane item, or None when the text is not a
    ``query_facts`` payload (older servers, other operations: rendered as-is)."""
    try:
        data = json.loads(entry_text or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or "facts" not in data:
        return None
    facts = data.get("facts")
    return facts if isinstance(facts, list) else None


def format_code_facts(facts: list) -> str:
    """One line per symbol, instead of the server's raw JSON.

    ``name [kind] file:line`` followed by the relations the graph recorded,
    ``→ relation target`` each. Fields a fact lacks are left out.
    """
    lines = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        name = str(fact.get("name") or fact.get("id") or "?")
        kind = str(fact.get("symbol_kind") or fact.get("kind") or "")
        where = str(fact.get("file") or "")
        line_no = fact.get("line")
        head = name
        if kind:
            head += f" [{kind}]"
        if where:
            head += f" {where}" + (f":{line_no}" if line_no not in (None, "") else "")
        lines.append(f"- {head}")
        for rel in fact.get("relations") or []:
            if not isinstance(rel, dict):
                continue
            rel_type = str(rel.get("type") or "")
            target = str(rel.get("target") or rel.get("target_id") or "")
            if rel_type or target:
                lines.append(f"  → {rel_type} {target}".rstrip())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Crediting memory in the answer (SDK-1071)
# ---------------------------------------------------------------------------
#
# Every recalled item the hook injects is marked with where it came from: a
# graph item with the dataset the server says it came from, ``[dataset
# <name>]`` (SDK-1393), any other item (or a graph item from a server that
# names no dataset) with its origin, ``[agent trace, …]``. An instruction above
# the memory asks the agent to end an answer that used memory with one
# ``> Cognee memory: …`` line. The recall hook records the turn it injected
# memory on; the Stop hook counts a credit when the answer on that turn ends
# with the credit line.

ENV_CITE_MEMORY = "COGNEE_PLUGIN_CITE_MEMORY"

#: Directory (under the suite's state dir) with one folder per session, shared
#: with the rest of the plugin's per-session state: its credit state
#: (``credit.json``) and running stored-item count (``stored.log``). The state
#: sweep removes folders idle for a week.
SESSION_STATE_DIRNAME = "sessions"
CREDIT_FILE = "credit.json"

_LEARNING_DATE_RE = re.compile(r"^# Session learning\W+(\d{4}-\d{2}-\d{2})", re.M)
#: The credit line, "> Cognee memory: …", as the answer's last line (the 🧠
#: older answers put before the label still counts).
_UNTAGGED_CREDIT_RE = re.compile(r"^\s*(?:>\s*)?(?:🧠\s*)?Cognee memory:\s*\S")
_AGENTS = {"claude": "Claude Code", "codex": "Codex", "antigravity": "Antigravity"}

#: Every graph item is marked with its dataset (SDK-1393); the credit line
#: names the datasets used.
CREDIT_INSTRUCTION = (
    "Crediting Cognee memory: Cognee found {found} for this prompt, each tagged below "
    "with the dataset it came from, as [dataset <name>]. If your answer uses "
    "information from a tagged item, end the answer with one line:\n"
    "> Cognee memory: <what you remembered>, from the <dataset> dataset\n"
    "Name each dataset you used once, and for an item tagged with no dataset say where "
    "it came from as its tag says. Never credit Cognee for what you know from project "
    "instruction files, your own memory files, the code, or this conversation. If no "
    "tagged item was used, do not mention memory."
)
FIRST_CREDIT_ADDENDUM = (
    "This would be the first Cognee credit in this session, so in that line also say in "
    "a few words how it connects to the earlier work."
)


def credit_enabled(default: bool = True) -> bool:
    """Whether credit marks and the resume notice are on.

    ``COGNEE_PLUGIN_CITE_MEMORY`` decides when set; otherwise ``default``, which
    each host passes (Codex starts with it off).
    """
    raw = os.environ.get(ENV_CITE_MEMORY, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _agent_name(session_id: str) -> str:
    prefix = session_id.split("_", 1)[0].lower() if "_" in session_id else ""
    return _AGENTS.get(prefix, "agent")


def passage_origin(passage: str) -> str:
    """Where a recalled passage came from, in a few words for its mark."""
    session_id = passage_session_id(passage)
    if not session_id:
        return "document"
    agent = _agent_name(session_id)
    date = _LEARNING_DATE_RE.search(passage.lstrip()[:400])
    if date:
        return f"{agent} session learning, {date.group(1)}"
    return f"{agent} session"


def item_mark(origin: str, dataset: str = "") -> str:
    """``[dataset <name>]`` for a graph item whose dataset the server named,
    ``[<origin>]`` for anything else (SDK-1393)."""
    if dataset:
        return f"[dataset {dataset}]"
    return f"[{origin}]" if origin else ""


def _safe_key(session_key: str) -> bool:
    return (
        bool(session_key)
        and bool(session_key.strip("."))
        and all(c.isalnum() or c in "._-" for c in session_key)
    )


def session_state_dir(state_dir, session_key: str):
    """The session's state folder, or ``None`` for a key unsafe as a file name."""
    if not _safe_key(session_key):
        return None
    return state_dir / SESSION_STATE_DIRNAME / session_key


def _read_json(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _update_credit_state(state_dir, session_key: str, change) -> None:
    folder = session_state_dir(state_dir, session_key)
    if folder:
        path = folder / CREDIT_FILE
        state = _read_json(path)
        change(state)
        _write_json(path, state)


def _turn(state: dict) -> int:
    try:
        return int(state.get("turn", 0) or 0)
    except (TypeError, ValueError):
        return 0


def begin_turn(state_dir, session_key: str) -> None:
    """Start a new turn before anything can fail: nothing is injected on it yet.

    The recall hook calls this first, so a prompt whose recall returned early
    (server down, no session, a timeout) is not an injected turn, and the Stop
    hook cannot take the previous turn's memory for this one's.
    """
    _update_credit_state(state_dir, session_key, lambda s: s.update(turn=_turn(s) + 1))


def mark_injected(state_dir, session_key: str) -> None:
    """Record that this turn injected memory with the credit instruction."""
    _update_credit_state(state_dir, session_key, lambda s: s.update(injected_turn=_turn(s)))


def injected_this_turn(state_dir, session_key: str) -> bool:
    """Whether the current turn injected memory (``mark_injected``)."""
    folder = session_state_dir(state_dir, session_key)
    if not folder:
        return False
    state = _read_json(folder / CREDIT_FILE)
    return "injected_turn" in state and state.get("injected_turn") == _turn(state)


def credit_instruction(first_in_session: bool, found: int = 0) -> str:
    noun = "1 memory" if found == 1 else f"{found} memories"
    text = CREDIT_INSTRUCTION.format(found=noun)
    if first_in_session:
        return f"{text} {FIRST_CREDIT_ADDENDUM}"
    return text


def has_credited(state_dir, session_key: str) -> bool:
    """Whether an answer in this session has already credited memory."""
    folder = session_state_dir(state_dir, session_key)
    return bool(folder and _read_json(folder / CREDIT_FILE).get("credited_turns"))


def check_credits(answer: str, injected: bool) -> dict:
    """Whether the answer ends with the credit line.

    Returns ``{"injected", "credited"}``: ``injected`` is whether this turn
    injected memory, ``credited`` whether the answer's last line is the credit
    line. A credit on a turn that injected nothing is reported but not counted.
    """
    last = (answer or "").rstrip().rsplit("\n", 1)[-1]
    return {"injected": bool(injected), "credited": bool(_UNTAGGED_CREDIT_RE.match(last))}


def record_credit(state_dir, session_key: str, result: dict, ts: str = "") -> None:
    """Count a turn whose answer credited the memory it was given."""
    folder = session_state_dir(state_dir, session_key)
    if not (folder and result.get("injected") and result.get("credited")):
        return
    path = folder / CREDIT_FILE
    state = _read_json(path)
    state["credited_turns"] = int(state.get("credited_turns", 0) or 0) + 1
    state.setdefault("first_credit_ts", ts)
    _write_json(path, state)


# ---------------------------------------------------------------------------
# "This conversation was saved" notice on resume (SDK-1071)
# ---------------------------------------------------------------------------
#
# The session-end sync runs after the chat is gone, so nothing it does can be
# shown there. When the user reopens the same chat (resume keeps the session
# id), session start reads that session's improve record and says, once per
# sync, whether the conversation was sent to long-term memory.

NOTICE_DIRNAME = "sync-notice"

_FAILURE_REASONS = {
    "unreachable": "the Cognee server was unreachable",
    "busy": "another save of this conversation was still running",
    "incomplete_drain": "part of it could not be uploaded",
}


def _clock(ts: float, now: float) -> str:
    """``14:32`` for today, ``Oct 3, 14:32`` for another day (local time)."""
    import time as _time

    when = _time.localtime(ts)
    if _time.strftime("%Y-%m-%d", when) == _time.strftime("%Y-%m-%d", _time.localtime(now)):
        return _time.strftime("%H:%M", when)
    return f"{_time.strftime('%b', when)} {when.tm_mday}, {_time.strftime('%H:%M', when)}"


def sync_notice(state: dict, now: float | None = None) -> tuple[str, float]:
    """``(message, sync_ts)`` for a session's last save; ``("", 0.0)`` when there is none.

    ``state`` is the plugin's per-session improve record (``last_improved_at``,
    ``trigger``, ``last_failed_at``, ``last_failure_reason``). ``sync_ts`` is the
    time of the save the message describes, so it is shown once per save.
    """
    import time as _time

    now = _time.time() if now is None else now
    try:
        ok_at = float(state.get("last_improved_at") or 0)
        failed_at = float(state.get("last_failed_at") or 0)
    except (TypeError, ValueError, AttributeError):
        return "", 0.0
    if failed_at > ok_at:
        reason = str(state.get("last_failure_reason") or "")
        why = _FAILURE_REASONS.get(reason) or (
            f"the save failed ({reason})" if reason else "the save failed"
        )
        left = "when you left at" if state.get("last_failure_trigger") == "final" else "at"
        where = _notice_dataset(state.get("dataset"))
        message = (
            f"This conversation wasn't saved to {where or 'Cognee'} {left} "
            f"{_clock(failed_at, now)}: {why}."
        )
        if ok_at:
            saved_to = _notice_dataset(state.get("improved_dataset"))
            into = f" to {saved_to}" if saved_to and saved_to != where else ""
            message += (
                f" An earlier save{into} at {_clock(ok_at, now)} has the conversation up to then."
            )
        return message, failed_at
    if ok_at:
        # "Sent", not "saved": the submit runs in the background, and a server
        # whose per-session lock was held skips it without saying so.
        left = "when you left at" if state.get("trigger") == "final" else "at"
        # An older record has only ``dataset``, written by the same save.
        where = _notice_dataset(state.get("improved_dataset") or state.get("dataset"))
        into = f"{where} in Cognee's long-term memory" if where else "Cognee's long-term memory"
        return f"This conversation was sent to {into} {left} {_clock(ok_at, now)}.", ok_at
    return "", 0.0


def _notice_dataset(dataset) -> str:
    """``the <name> dataset`` for the save notice; ``""`` when the record names none."""
    name = str(dataset or "").strip()
    return f"the {name} dataset" if name else ""


def _notice_path(state_dir, session_id: str):
    import hashlib

    digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()
    return state_dir / NOTICE_DIRNAME / f"{digest}.json"


def notice_due(state_dir, session_id: str, sync_ts: float) -> bool:
    """Whether the notice for the save at ``sync_ts`` has not been shown yet."""
    if not session_id or not sync_ts:
        return False
    try:
        shown = float(_read_json(_notice_path(state_dir, session_id)).get("shown_for") or 0)
    except (TypeError, ValueError):
        shown = 0.0
    return shown < sync_ts


def mark_notice_shown(state_dir, session_id: str, sync_ts: float) -> None:
    if session_id:
        _write_json(_notice_path(state_dir, session_id), {"shown_for": sync_ts})
