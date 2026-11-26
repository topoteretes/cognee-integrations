#!/usr/bin/env python3
"""Propose architecture rules mined from this repository's code graph.

Usage:
  cognee-rules.py list [--limit <n>] [--min-confidence <0..1>] [--min-support <n>] [--json]
  cognee-rules.py accept <id> [--name <file-name>]
  cognee-rules.py reject <id> [<id> ...]

list:    the server mines candidate rules (enola `constraints mine`) from the
         repository's snapshot and lints each one shown as a draft (enola
         `constraints lint`). Every candidate is printed with its id, its
         support (how many facts follow it), its exceptions, the lint result
         (a candidate that fails lint is shown as failing, with the reason),
         and the stored `constraint` / `convention` lessons that mention the
         same files or symbols; candidates with such lessons come first.
         --limit (default 20) bounds how many are linted and shown; the rest
         are counted. Candidates rejected earlier are not proposed again.
accept:  write the candidate's rule to enola/constraints/<name>.yaml in the
         working tree. Nothing is committed and nothing is sent to the server.
         Only after the person said yes to that candidate.
reject:  remember the candidate so `list` does not propose it again.

The Cognee server runs enola; this script never does. It needs a local server
with the code analysis API and a repository indexed by path.

Exit status: 0 done, 1 bad arguments or an unknown id, 2 the server could not
do it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _code_analysis

FEATURE = "rules"
DEFAULT_LIMIT = 20
RULES_DIRECTORY = "enola/constraints"
LESSON_TYPES = ("constraint", "convention")
_LINT_TIMEOUT = 120.0


def candidate_id(candidate: dict) -> str:
    """A short stable id of a mined candidate (enola's own ``identity``, hashed)."""
    identity = str(candidate.get("identity") or candidate.get("yaml") or "")
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]


def _state_path(repo_root: str):
    return _code_analysis.repo_state_path(repo_root, "rules-")


def rule_name(candidate: dict) -> str:
    """The file name of a candidate's rule: its first rule id, made path-safe."""
    match = re.search(r'^\s*-\s*id:\s*"?([^"\n]+)"?\s*$', str(candidate.get("yaml") or ""), re.M)
    name = match.group(1) if match else "mined-" + candidate_id(candidate)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "mined-" + candidate_id(candidate)


def lint_problems(text: str) -> list:
    """The validation problems of a lint report, without the server's scratch path."""
    problems = []
    for line in str(text or "").splitlines():
        line = line.strip()
        found = re.match(r"^\S*declaration\.yaml: (.*)$", line)
        if found:
            if not re.match(r"^\d+ component\(s\)", found.group(1)):
                problems.append(found.group(1))
        elif line.startswith("--fail-empty:") or "matches nothing" in line:
            problems.append(line)
    if not problems:
        problems = [line.strip() for line in str(text or "").splitlines() if "FAIL" in line]
    return problems or ["lint reported problems (no detail)"]


def _candidate_terms(candidate: dict) -> tuple:
    """(files, symbol names) a candidate is about: its exceptions and witnesses."""
    files, names = set(), set()
    for item in (candidate.get("exceptions") or []) + (candidate.get("witnesses") or []):
        if not isinstance(item, dict):
            continue
        if item.get("file"):
            files.add(str(item["file"]).lower())
        name = str(item.get("name") or "")
        short = name.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
        if len(short) >= 4:
            names.add(short.lower())
    return files, names


def related_lessons(candidate: dict, lessons: list) -> list:
    """The constraint / convention lessons that mention the candidate's files or symbols."""
    files, names = _candidate_terms(candidate)
    out = []
    for lesson in lessons:
        lesson_files = {str(f).lower() for f in lesson.get("files") or []}
        lesson_names = {
            str(s.get("name") or "").rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower()
            for s in lesson.get("symbols") or []
            if isinstance(s, dict)
        }
        body = str(lesson.get("body") or "").lower()
        same_file = any(
            f == lf or f.endswith("/" + lf) or lf.endswith("/" + f)
            for f in files
            for lf in lesson_files
        )
        same_name = bool(names & lesson_names) or any(
            re.search(r"(?<![a-z0-9_])" + re.escape(n) + r"(?![a-z0-9_])", body) for n in names
        )
        if same_file or same_name:
            out.append(lesson)
    return out


def stored_lessons(cwd: str) -> tuple:
    """(constraint / convention lessons of this project, note). The note says why
    there are none to compare with; lessons exist only with coding memory on."""
    previous = os.environ.get("COGNEE_PLUGIN_IN_VENV")
    os.environ["COGNEE_PLUGIN_IN_VENV"] = "1"
    try:
        import _coding_memory

        if not _coding_memory.enabled():
            return [], "no stored lessons to compare with (coding memory is off)"
        session_id, dataset = _coding_memory._session()
        if not dataset:
            return [], "no stored lessons to compare with (no dataset for this launch)"
        project = _coding_memory.project_scope(dataset, session_id, cwd)
        _, lessons = _coding_memory.list_lessons(dataset, project, time.monotonic() + 15.0)
    except Exception as exc:
        return [], "stored lessons could not be listed (%s)" % str(exc)[:120]
    finally:
        if previous is None:
            os.environ.pop("COGNEE_PLUGIN_IN_VENV", None)
        else:
            os.environ["COGNEE_PLUGIN_IN_VENV"] = previous
    kept = [lesson for lesson in lessons if lesson.get("lesson_type") in LESSON_TYPES]
    return kept, "" if kept else "no stored constraint or convention lessons for this project"


def mine(rt: dict, state: dict, params: dict, *, opener=None) -> dict:
    answer = _code_analysis.analyze(
        rt, FEATURE, "constraints_mine", params, state["dataset"], opener=opener
    )
    if not answer["ok"]:
        return answer
    records = answer["result"].get("output")
    records = records if isinstance(records, list) else []
    return {
        "ok": True,
        "candidates": [
            r
            for r in records
            if isinstance(r, dict) and r.get("type") == "candidate" and r.get("yaml")
        ],
        "suppressed": [r for r in records if isinstance(r, dict) and r.get("type") == "suppressed"],
    }


def lint(rt: dict, state: dict, candidate: dict, *, opener=None) -> dict:
    """``{"ok": bool, "problems": [...]}`` for one candidate's draft."""
    answer = _code_analysis.analyze(
        rt,
        FEATURE,
        "constraints_lint",
        {"declaration": candidate["yaml"], "fail_empty": True},
        state["dataset"],
        timeout=_LINT_TIMEOUT,
        opener=opener,
    )
    if not answer["ok"]:
        return {"ok": False, "problems": ["lint could not run: " + answer["message"]]}
    result = answer["result"]
    if result.get("exit_code") == 0:
        return {"ok": True, "problems": []}
    return {"ok": False, "problems": lint_problems(result.get("output"))}


def propose(cwd: str, rt: dict, params: dict, limit: int, lessons: list, *, opener=None) -> dict:
    """Mine, rank (candidates with related lessons first), lint the ones shown,
    and remember them so ``accept`` needs no second mining run."""
    state, message = _code_analysis.indexed_repo(cwd, FEATURE, rt)
    if not state:
        return {"ok": False, "code": "not_indexed_by_path", "message": message}
    mined = mine(rt, state, params, opener=opener)
    if not mined["ok"]:
        return mined
    saved = _code_analysis.read_json(_state_path(state["repo_root"]))
    rejected = set(saved.get("rejected") or [])
    entries = []
    for candidate in mined["candidates"]:
        if candidate_id(candidate) in rejected:
            continue
        entries.append(
            {
                "id": candidate_id(candidate),
                "candidate": candidate,
                "lessons": related_lessons(candidate, lessons),
            }
        )
    # Stable: enola's own rank decides inside each group.
    entries.sort(key=lambda entry: (not entry["lessons"], entry["candidate"].get("rank") or 0))
    shown = entries[: max(1, limit)]
    for entry in shown:
        entry["lint"] = lint(rt, state, entry["candidate"], opener=opener)
    saved["rejected"] = sorted(rejected)
    saved["candidates"] = {
        entry["id"]: {"candidate": entry["candidate"], "lint": entry["lint"]} for entry in shown
    }
    _code_analysis.write_json(_state_path(state["repo_root"]), saved)
    return {
        "ok": True,
        "repo_root": state["repo_root"],
        "shown": shown,
        "not_shown": len(entries) - len(shown),
        "rejected": len(mined["candidates"]) - len(entries),
        "suppressed": mined["suppressed"],
    }


def _print(result: dict, note: str) -> None:
    shown = result["shown"]
    print(
        "Cognee rules: %d candidate rule(s) for %s%s%s"
        % (
            len(shown),
            os.path.basename(result["repo_root"]),
            ", %d more not shown (raise --limit)" % result["not_shown"]
            if result["not_shown"]
            else "",
            ", %d rejected earlier" % result["rejected"] if result["rejected"] else "",
        )
    )
    if note:
        print("Lessons: " + note)
    for entry in shown:
        candidate, verdict = entry["candidate"], entry["lint"]
        print()
        print("[%s] %s" % (entry["id"], candidate.get("statement") or rule_name(candidate)))
        print(
            "  support: %s/%s (confidence %.2f), family %s"
            % (
                candidate.get("numerator", "?"),
                candidate.get("denominator", "?"),
                float(candidate.get("confidence") or 0),
                candidate.get("family") or "?",
            )
        )
        exceptions = [e for e in candidate.get("exceptions") or [] if isinstance(e, dict)]
        print("  exceptions (%d):%s" % (len(exceptions), "" if exceptions else " none"))
        for item in exceptions:
            print("    - %s (%s): %s" % (item.get("name"), item.get("file"), item.get("detail")))
        if verdict["ok"]:
            print("  lint: passes")
        else:
            print("  lint: FAILS")
            for problem in verdict["problems"]:
                print("    - " + problem)
        for lesson in entry["lessons"]:
            body = " ".join(str(lesson.get("body") or "").split())
            print("  lesson (%s): %s" % (lesson.get("lesson_type"), body[:300]))
        print("  rule file: %s/%s.yaml" % (RULES_DIRECTORY, rule_name(candidate)))
    if shown:
        print()
        print(
            "Accept one with: cognee-rules.sh accept <id>; reject with: cognee-rules.sh reject <id>"
        )


def accept(cwd: str, rt: dict, wanted: str, name: str = "") -> tuple:
    """Write the candidate's rule file. ``(exit status, message)``."""
    state, message = _code_analysis.indexed_repo(cwd, FEATURE, rt)
    if not state:
        return 2, message
    saved = _code_analysis.read_json(_state_path(state["repo_root"]))
    entry = (saved.get("candidates") or {}).get(wanted)
    if not isinstance(entry, dict):
        return (
            1,
            "Cognee rules: no candidate %s in the last list; run `cognee-rules.sh list`." % wanted,
        )
    if not (entry.get("lint") or {}).get("ok"):
        problems = "; ".join((entry.get("lint") or {}).get("problems") or [])
        return 1, "Cognee rules: candidate %s fails lint and was not written: %s" % (
            wanted,
            problems,
        )
    candidate = entry["candidate"]
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or rule_name(candidate)
    name = name[: -len(".yaml")] if name.endswith(".yaml") else name
    path = os.path.join(state["repo_root"], *RULES_DIRECTORY.split("/"), name + ".yaml")
    if os.path.exists(path):
        return 1, "Cognee rules: %s already exists; pass --name <other-name>." % path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(candidate["yaml"])
    return 0, (
        "Cognee rules: wrote %s (not committed). Its `because` text is the mined statement: "
        "rewrite it, and set mode to enforce it." % path
    )


def reject(cwd: str, rt: dict, ids: list) -> tuple:
    state, message = _code_analysis.indexed_repo(cwd, FEATURE, rt)
    if not state:
        return 2, message
    saved = _code_analysis.read_json(_state_path(state["repo_root"]))
    known = saved.get("candidates") or {}
    unknown = [i for i in ids if i not in known and i not in (saved.get("rejected") or [])]
    if unknown or not ids:
        return 1, "Cognee rules: no candidate %s in the last list." % ", ".join(
            unknown or ["given"]
        )
    saved["rejected"] = sorted(set(saved.get("rejected") or []) | set(ids))
    saved["candidates"] = {k: v for k, v in known.items() if k not in ids}
    _code_analysis.write_json(_state_path(state["repo_root"]), saved)
    return 0, "Cognee rules: %s will not be proposed again." % ", ".join(ids)


def _options(argv: list) -> tuple:
    values, flags, rest = {}, set(), []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--limit", "--min-confidence", "--min-support", "--name"):
            index += 1
            values[arg] = argv[index] if index < len(argv) else ""
        elif arg.startswith("--"):
            flags.add(arg)
        else:
            rest.append(arg)
        index += 1
    return values, flags, rest


def main(argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 1
    command = argv[0]
    try:
        values, flags, rest = _options(argv[1:])
        limit = int(values.get("--limit") or DEFAULT_LIMIT)
        params = {}
        if values.get("--min-confidence"):
            params["min_confidence"] = float(values["--min-confidence"])
        if values.get("--min-support"):
            params["min_support"] = int(values["--min-support"])
    except ValueError as exc:
        print("Cognee rules: bad argument (%s); see --help" % exc, file=sys.stderr)
        return 1
    cwd = os.getcwd()
    rt = _code_analysis.runtime()
    if command == "list":
        lessons, note = stored_lessons(cwd)
        result = propose(cwd, rt, params, limit, lessons)
        if not result["ok"]:
            print(result["message"])
            return 2
        if "--json" in flags:
            print(
                json.dumps({k: result[k] for k in ("shown", "not_shown", "rejected", "suppressed")})
            )
        else:
            _print(result, note)
        return 0
    if command == "accept" and len(rest) == 1:
        status, message = accept(cwd, rt, rest[0], values.get("--name") or "")
    elif command == "reject" and rest:
        status, message = reject(cwd, rt, rest)
    else:
        print(
            "Cognee rules: usage: list | accept <id> | reject <id> ... (see --help)",
            file=sys.stderr,
        )
        return 1
    print(message)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
