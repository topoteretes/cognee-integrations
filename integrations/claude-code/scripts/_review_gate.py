#!/usr/bin/env python3
"""The gate of the automatic code review (``auto-review.py``).

Decides, without an LLM and without a request, whether a Bash call just
published or recorded a change that is worth a review. ``decide`` answers
``{"run": False, "reason": ...}`` or ``{"run": True, "repo_root", "base",
"base_commit", "head", "trigger"}``. In order:

1. ``COGNEE_AUTO_REVIEW`` is not turned off and coding memory is on.
2. The call is a Bash call that did not fail.
3. Its command runs ``git commit``, ``git push`` or ``gh pr create``
   (``triggers``): the command line is parsed, so the words inside a quoted
   string, a heredoc body, a comment or a ``bash -c`` script do not count, and
   neither do ``--dry-run`` / ``--help`` runs. ``gh pr merge`` is not a trigger.
4. The directory the command ran in (after its ``cd`` and ``git -C``) is inside
   a git repository this plugin indexed by path.
5. The server is not known to lack the analyze route (the cached capability;
   an unknown is settled by the review itself).
6. A base exists: the remote default branch, else the branch's upstream.
7. The change holds code files (the extensions the code graph indexes): the
   commit's own files for ``git commit``, the files since the merge-base
   otherwise.
8. No automatic review of this HEAD against this merge-base ran or is running
   (``claim``; the state file sits next to the review marker).
"""

from __future__ import annotations

import os
import time
from pathlib import PurePosixPath

import _code_analysis
import _code_graph
from _capture_policy import _command_words

ENV_AUTO_REVIEW = "COGNEE_AUTO_REVIEW"
_FALSE = {"0", "false", "no", "off"}

#: Words that may stand in front of a command without changing what runs.
_PREFIX_WORDS = {"{", "}", "!", "then", "do", "else", "elif", "if", "while", "until", "time"}
_WRAPPERS = {"nohup", "rtk", "proxy"}
#: git options that take their value as the next word.
_GIT_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
_GH_VALUE_OPTIONS = {"-R", "--repo"}
_SEPARATORS = ";&|()"


def enabled() -> bool:
    return os.environ.get(ENV_AUTO_REVIEW, "").strip().lower() not in _FALSE


# -- Command line --------------------------------------------------------------------


def _skip_heredocs(text: str, index: int, pending: list) -> int:
    """``index`` is just past a newline: skip the bodies of the pending heredocs."""
    for delimiter, strip_tabs in pending:
        while index < len(text):
            end = text.find("\n", index)
            line = text[index:] if end < 0 else text[index:end]
            index = len(text) if end < 0 else end + 1
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
    pending.clear()
    return index


def _heredoc_delimiter(text: str, index: int) -> tuple:
    """``index`` is past ``<<``: ``(delimiter, strip_tabs, index after it)``."""
    strip_tabs = text.startswith("-", index)
    index += strip_tabs
    while index < len(text) and text[index] in " \t":
        index += 1
    word = ""
    while index < len(text) and text[index] not in " \t\n;&|()<>":
        char = text[index]
        if char in "'\"":
            end = text.find(char, index + 1)
            end = len(text) if end < 0 else end
            word += text[index + 1 : end]
            index = end + 1
        elif char == "\\" and index + 1 < len(text):
            word += text[index + 1]
            index += 2
        else:
            word += char
            index += 1
    return word, bool(strip_tabs), index


def _scan(text: str, index: int = 0, closer: str = "") -> tuple:
    """The simple commands of ``text`` from ``index``, each a list of words, and
    where the scan stopped (past ``closer`` inside a ``$(...)``).

    Quotes are removed, so a quoted string is one word. A command substitution
    is one opaque word (its commands are scanned to find its end and dropped),
    a heredoc body is skipped, and a comment ends at its line.
    """
    commands: list = []
    words: list = []
    word = None  # None between words, "" for an empty quoted word
    heredocs: list = []

    def end_word():
        nonlocal word
        if word is not None:
            words.append(word)
        word = None

    def end_command():
        nonlocal words
        end_word()
        if words:
            commands.append(words)
        words = []

    while index < len(text):
        char = text[index]
        if char == "\\":
            if text[index + 1 : index + 2] != "\n":
                word = (word or "") + text[index + 1 : index + 2]
            index += 2
        elif char == "'":
            end = text.find("'", index + 1)
            end = len(text) if end < 0 else end
            word = (word or "") + text[index + 1 : end]
            index = end + 1
        elif char == '"':
            word = word or ""
            index += 1
            while index < len(text) and text[index] != '"':
                if text[index] == "\\":
                    word += text[index + 1 : index + 2]
                    index += 2
                elif text.startswith("$(", index):
                    _, index = _scan(text, index + 2, ")")
                    word += "$()"
                else:
                    word += text[index]
                    index += 1
            index += 1
        elif text.startswith("$(", index):
            _, index = _scan(text, index + 2, ")")
            word = (word or "") + "$()"
        elif char == "`":
            end = text.find("`", index + 1)
            index = (len(text) if end < 0 else end) + 1
            word = (word or "") + "$()"
        elif char == "#" and word is None:
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        elif text.startswith("<<", index) and not text.startswith("<<<", index):
            end_word()
            delimiter, strip_tabs, index = _heredoc_delimiter(text, index + 2)
            if delimiter:
                heredocs.append((delimiter, strip_tabs))
        elif char == "\n":
            end_command()
            index = _skip_heredocs(text, index + 1, heredocs)
        elif closer and char == closer:
            end_command()
            return commands, index + 1
        elif char in _SEPARATORS:
            end_command()
            index += 1
        elif char in " \t\r<>":
            end_word()
            index += 1
        else:
            word = (word or "") + char
            index += 1
    end_command()
    return commands, index


def _past_options(words: list, start: int, value_options: set) -> int:
    """The index of the first word after the leading options from ``start``."""
    index = start
    while index < len(words) and words[index].startswith("-"):
        index += 2 if words[index] in value_options else 1
    return index


def _flags(args: list) -> set:
    """The options of a subcommand, up to ``--``."""
    return set(args[: args.index("--")] if "--" in args else args)


def triggers(command: str) -> list:
    """The publishing commands ``command`` runs: ``[{"kind", "dir"}]``.

    ``kind`` is ``commit``, ``push`` or ``pr_create``; ``dir`` the directory it
    ran in relative to the call's working directory ("" for that directory),
    from the ``cd`` commands before it and git's ``-C``.
    """
    try:
        commands, _ = _scan(str(command or ""))
    except Exception:
        return []
    found = []
    directory = ""
    for words in commands:
        while words and words[0] in _PREFIX_WORDS:
            words = words[1:]
        words = _command_words(words)
        while words and PurePosixPath(words[0]).name in _WRAPPERS:
            words = _command_words(words[1:])
        if not words:
            continue
        name = PurePosixPath(words[0]).name
        if name in ("cd", "pushd"):
            target = [w for w in words[1:] if not w.startswith("-")]
            if target:
                directory = os.path.join(directory, os.path.expanduser(target[0]))
            continue
        if name == "git":
            where = directory
            index = 1
            while index < len(words) and words[index].startswith("-"):
                if words[index] == "-C" and index + 1 < len(words):
                    where = os.path.join(where, os.path.expanduser(words[index + 1]))
                index += 2 if words[index] in _GIT_VALUE_OPTIONS else 1
            sub, flags = (words[index : index + 1] or [""])[0], _flags(words[index + 1 :])
            if flags & {"--dry-run", "--help", "-h"}:
                continue
            if sub == "commit":
                found.append({"kind": "commit", "dir": where})
            elif sub == "push" and not flags & {"-n", "--delete", "-d"}:
                found.append({"kind": "push", "dir": where})
        elif name == "gh":
            index = _past_options(words, 1, _GH_VALUE_OPTIONS)
            if words[index : index + 2] == ["pr", "create"] and not (
                _flags(words[index + 2 :]) & {"--dry-run", "--help", "-h"}
            ):
                found.append({"kind": "pr_create", "dir": directory})
    return found


def succeeded(payload: dict) -> bool:
    """False when the tool response says the call failed or was interrupted."""
    response = payload.get("tool_response")
    if not isinstance(response, dict):
        return True
    if response.get("interrupted") or response.get("is_error") or response.get("error"):
        return False
    return response.get("exit_code", response.get("exitCode", 0)) in (0, "0", None)


# -- Repository ----------------------------------------------------------------------


def base_ref(repo_root: str) -> str:
    """The remote default branch, else the current branch's upstream, else ""."""
    base = _code_graph._run_git(
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], repo_root
    ).strip()
    if base:
        return base
    return _code_graph._run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], repo_root
    ).strip()


def code_files(paths) -> list:
    return [
        path
        for path in paths
        if PurePosixPath(path).suffix.lower().lstrip(".") in _code_graph.CODE_EXTENSIONS
    ]


def changed_files(repo_root: str, kind: str, base_commit: str) -> list:
    """The files of the HEAD commit (``commit``) or since the merge-base."""
    if kind == "commit":
        args = ["diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "--root", "HEAD"]
    else:
        args = ["diff", "--name-only", base_commit, "HEAD"]
    return [line for line in _code_graph._run_git(args, repo_root).splitlines() if line.strip()]


# -- Once per commit -----------------------------------------------------------------


def state_path(repo_root: str):
    return _code_analysis.repo_state_path(repo_root, prefix="auto-")


def claim(repo_root: str, head: str, base_commit: str) -> str:
    """Record that a review of ``head`` against ``base_commit`` starts now.

    "" when claimed, else why not: ``already_reviewed``, or ``already_running``
    while the process of an unfinished one is alive.
    """
    from _file_lock import file_lock
    from _proc import pid_alive

    path = state_path(repo_root)
    with file_lock(path.with_suffix(".lock"), timeout=1.0) as acquired:
        if not acquired:
            return "state_locked"
        state = _code_analysis.read_json(path)
        if state.get("head") == head and state.get("base_commit") == base_commit:
            if state.get("status") == "done":
                return "already_reviewed"
            if state.get("status") == "running" and pid_alive(int(state.get("pid") or 0)):
                return "already_running"
        _code_analysis.write_json(
            path,
            {
                "head": head,
                "base_commit": base_commit,
                "status": "running",
                "pid": os.getpid(),
                "started_at": time.time(),
            },
        )
    return ""


def settle(repo_root: str, **changes) -> None:
    """Update the claim (the reviewing process's pid, then its outcome)."""
    path = state_path(repo_root)
    state = _code_analysis.read_json(path)
    state.update(changes)
    _code_analysis.write_json(path, state)


# -- Decision ------------------------------------------------------------------------


def _no(reason: str, **detail) -> dict:
    return {"run": False, "reason": reason, **detail}


def decide(payload: dict, *, service_url=None) -> dict:
    """Whether to review after this tool call (see the module docstring).

    ``service_url`` is a callable naming the server, asked only once the
    command is a trigger in an indexed repository."""
    if not enabled():
        return _no("disabled")
    from config import coding_memory_enabled

    if not coding_memory_enabled():
        return _no("coding_memory_off")
    if str(payload.get("tool_name") or "") != "Bash":
        return _no("not_bash")
    if not succeeded(payload):
        return _no("call_failed")
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else ""
    found = triggers(command if isinstance(command, str) else "")
    if not found:
        return _no("not_a_trigger")
    cwd = str(payload.get("cwd") or "") or os.getcwd()
    reason = "not_in_git"
    for trigger in found:
        outcome = _decide_one(trigger, cwd, service_url)
        if outcome["run"]:
            return outcome
        reason = outcome["reason"]
    return _no(reason, trigger=found[0]["kind"])


def _decide_one(trigger: dict, cwd: str, service_url) -> dict:
    kind = trigger["kind"]
    directory = os.path.normpath(os.path.join(cwd, trigger["dir"])) if trigger["dir"] else cwd
    repo_root = _code_graph.git_repo_root(directory)
    if not repo_root:
        return _no("not_in_git")
    state = _code_graph.find_indexed_repo(repo_root)
    if state.get("spec_kind") != "path" or not state.get("dataset"):
        return _no("not_indexed_by_path")
    repo_root = str(state["repo_root"])
    url = service_url() if callable(service_url) else str(service_url or "")
    if url and _code_analysis._cached_capability(url, "analyze") is False:
        return _no("server_unsupported")
    base = base_ref(repo_root)
    if not base:
        return _no("no_base")
    head = _code_graph._run_git(["rev-parse", "HEAD"], repo_root).strip()
    base_commit = _code_graph._run_git(["merge-base", base, "HEAD"], repo_root).strip()
    if not head or not base_commit:
        return _no("no_base")
    if not code_files(changed_files(repo_root, kind, base_commit)):
        return _no("no_code_change")
    refused = claim(repo_root, head, base_commit)
    if refused:
        return _no(refused)
    return {
        "run": True,
        "trigger": kind,
        "repo_root": repo_root,
        "base": base,
        "base_commit": base_commit,
        "head": head,
    }
