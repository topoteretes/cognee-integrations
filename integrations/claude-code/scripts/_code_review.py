#!/usr/bin/env python3
"""Architecture review of a change (enola ``check``, run by the Cognee server).

Three parts, shared by ``cognee-review.py`` and the prompt hook:

* ``run_review`` asks the server to grade the working tree of the checkout's
  path-indexed repository against a base ref. The server stores the verdict on
  the repository's code graph.
* ``report`` renders a verdict as a short text report.
* ``where_to_look`` composes, from the server's other analyses, the places a
  reviewer should read first: the symbols the change touched (``locate`` over
  its diff), how much depends on each (``impact``), which were changed by fix
  commits before (``touched``), and the new findings, resolved findings and
  removed symbols of the verdict. Every extra call is optional and they run one
  after another inside one time budget (each takes the dataset lock).
* ``prompt_notice`` is the prompt hook's part. A review run by the script leaves
  a marker for its repository; while the marker is pending, the next prompt
  reads the stored verdict with the ``review`` CODE operation and, when it
  reports a regression or changed rule files, returns a short block once for
  that verdict. Without a marker it costs one file read and no request. A
  review the gate ran by itself (``auto-review.py``) also leaves its "Where to
  look" lines in the marker, and those are shown once even when the verdict is
  clean.

Nothing here runs enola, and nothing here decides to run a review.
"""

from __future__ import annotations

import os
import time

import _code_analysis
import _code_graph

FEATURE = "review"
#: Seconds the prompt hook waits for the stored verdict.
NOTICE_TIMEOUT_SECONDS = 2.0
#: Prompts that may fail to read the verdict before the marker is dropped.
NOTICE_MAX_ATTEMPTS = 3
NOTICE_MAX_CHARS = 900
_MAX_LISTED = 8
#: Places a "Where to look" section lists.
MAX_PLACES = 5
#: Changed symbols ``impact`` is asked for (one server call each), largest first.
IMPACT_SYMBOLS = 5
#: Seconds the extra analyses of ``where_to_look`` may take in all.
WHERE_BUDGET_SECONDS = 60.0
#: Seconds a review waits for the re-index of a working tree edited since the last one.
REFRESH_TIMEOUT_SECONDS = 60.0
#: Commit subjects that count as a fix (``touched``'s subject filter, a regular expression).
FIX_SUBJECT = "(?i)\\bfix"
_PLACE_LINE_CHARS = 220


def default_base(repo_root: str) -> str:
    """The remote default branch (``origin/main``), or "" when git names none."""
    return _code_graph._run_git(
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], repo_root
    ).strip()


def _marker_path(repo_root: str):
    return _code_analysis.repo_state_path(repo_root)


def run_review(
    cwd: str,
    base: str,
    params: dict,
    rt: dict,
    *,
    opener=None,
    timeout: float = 600.0,
    refresh: bool = False,
) -> dict:
    """``{"ok": True, "result", "state", "graph_lag"}`` or ``{"ok": False, "message", "code"}``.

    With ``refresh``, a working tree edited since the last index is re-indexed
    before the review, so the verdict and the analyses that follow it describe
    the same graph; ``graph_lag`` says why when that could not be done ("" otherwise).
    """
    state, message = _code_analysis.indexed_repo(cwd, FEATURE, rt)
    if not state:
        return {"ok": False, "code": "not_indexed_by_path", "message": message}
    base = base or default_base(state["repo_root"])
    if not base:
        return {
            "ok": False,
            "code": "no_base",
            "message": "Cognee review: name the base ref to compare against (for example "
            "`cognee-review.sh main`); this checkout has no remote default branch "
            "(refs/remotes/origin/HEAD).",
        }
    lag = ""
    if refresh:
        try:
            lag = _refresh_graph(state, rt, REFRESH_TIMEOUT_SECONDS, opener)
        except Exception as exc:
            lag = str(exc)[:160]
    answer = _code_analysis.analyze(
        rt,
        FEATURE,
        "check",
        {"base": base, **params},
        state["dataset"],
        timeout=timeout,
        opener=opener,
    )
    if not answer["ok"]:
        return answer
    review = answer["result"].get("review") or {}
    if review.get("reviewed_at"):
        marker = _code_analysis.read_json(_marker_path(state["repo_root"]))
        # A new verdict: the lines an earlier automatic review left are not its.
        marker.update(pending=str(review["reviewed_at"]), attempts=0, where=[], where_for="")
        _code_analysis.write_json(_marker_path(state["repo_root"]), marker)
    return {"ok": True, "result": answer["result"], "state": state, "graph_lag": lag}


def stored_review(cwd: str, rt: dict, *, timeout=30.0, opener=None) -> dict:
    """The verdict the server stored for this checkout's repository:
    ``{"ok": True, "review": dict | None}`` or a failure."""
    state, message = _code_analysis.indexed_repo(cwd, FEATURE, rt)
    if not state:
        return {"ok": False, "code": "not_indexed_by_path", "message": message}
    return _read_review(state, rt, timeout=timeout, opener=opener)


def _read_review(state: dict, rt: dict, *, timeout, opener=None) -> dict:
    repo = os.path.basename(str(state["repo_root"]).rstrip(os.sep))
    answer = _code_analysis.code_operation(
        rt, state["dataset"], {"operation": "review", "repo": repo}, timeout=timeout, opener=opener
    )
    if not answer["ok"]:
        if answer["code"] == "unknown_operation":
            answer["message"] = _code_analysis.unsupported_message(
                FEATURE, rt, "this server has no review CODE operation"
            )
        else:
            answer["message"] = "Cognee review: could not read the stored verdict: " + str(
                answer["message"]
            )
        return answer
    reviews = [r for r in answer["raw"].get("reviews") or [] if isinstance(r, dict)]
    exact = [r for r in reviews if r.get("repo") == repo]
    return {"ok": True, "review": (exact or reviews or [None])[0]}


# -- Report ----------------------------------------------------------------------


def _counts(mapping) -> str:
    if not isinstance(mapping, dict) or not mapping:
        return ""
    items = sorted(mapping.items(), key=lambda item: (-int(item[1] or 0), str(item[0])))
    return ", ".join("%s %s" % (name, count) for name, count in items)


def _finding_line(finding: dict) -> str:
    where = ""
    for evidence in finding.get("evidence") or []:
        if isinstance(evidence, dict) and evidence.get("file"):
            where = " (%s%s)" % (
                evidence["file"],
                ":%s" % evidence["line"] if evidence.get("line") else "",
            )
            break
    confidence = finding.get("confidence")
    sure = ", confidence %s" % confidence if isinstance(confidence, (int, float)) else ""
    return "[%s] %s%s%s" % (
        finding.get("source") or "?",
        finding.get("title") or "",
        where,
        sure,
    )


def below_floor_line(verdict: dict) -> str:
    """One line when findings of the --fail-on explainers did not count because enola
    was less sure of them than the policy's confidence floor; "" when none."""
    policy = verdict.get("policy") or {}
    explainers = policy.get("fail_explainers") or []
    floor = policy.get("min_confidence")
    if not explainers or not isinstance(floor, (int, float)):
        return ""
    confidences = [
        finding["confidence"]
        for finding in (verdict.get("diff") or {}).get("findings_new") or []
        if isinstance(finding, dict)
        and finding.get("source") in explainers
        and isinstance(finding.get("confidence"), (int, float))
        and finding["confidence"] < floor
    ]
    if not confidences:
        return ""
    return (
        "%d new finding(s) of the --fail-on explainers did not count: enola's confidence "
        "in them (highest %s) is below the floor %s. Pass --min-confidence %s to count them."
        % (len(confidences), max(confidences), floor, max(confidences))
    )


def is_regression(verdict: dict, exit_code) -> bool:
    return str(verdict.get("status") or "").lower() == "regression" or exit_code == 1


def _scope_lines(verdict: dict) -> list:
    """What the change reached outside the scope it declared (``target``/``expected``)."""
    lines = []
    breached = {
        str((b.get("measurement") or {}).get("name") or "")
        for b in verdict.get("breaches") or []
        if isinstance(b, dict)
    }
    for measurement in verdict.get("measurements") or []:
        if not isinstance(measurement, dict) or not measurement.get("count"):
            continue
        line = "%s %s" % (measurement["count"], measurement.get("label") or measurement.get("name"))
        if measurement.get("name") in breached:
            line += " (over the limit)"
        lines.append(line)
    spillover = verdict.get("spillover")
    if isinstance(spillover, list) and spillover:
        names = [s if isinstance(s, str) else str(s.get("name") or s) for s in spillover]
        lines.append("reached: " + ", ".join(names[:_MAX_LISTED]))
    return lines


def status_line(verdict: dict, exit_code) -> str:
    status = str(verdict.get("status") or "unknown")
    if exit_code == 3:
        reason = ((verdict.get("diff") or {}).get("comparability") or {}).get("reason") or ""
        return "Status: DECLINED, the base is not comparable%s" % (": " + reason if reason else "")
    if is_regression(verdict, exit_code):
        return "Status: REGRESSION (the policy given with --fail-on / --max-spillover failed)"
    return "Status: %s" % status


def report(review: dict, verdict: dict, exit_code, enola_version: str = "") -> str:
    """The compact report of one verdict. ``review`` is the stored record (repo,
    base, base_commit, reviewed_at, rule_files_changed and, when read back, the
    current graph snapshot)."""
    lines = [
        "Cognee review: %s against %s (merge-base %s)%s"
        % (
            review.get("repo") or "repository",
            review.get("base") or "?",
            str(review.get("base_commit") or "?")[:10],
            ", enola " + enola_version if enola_version else "",
        ),
        status_line(verdict, exit_code),
    ]
    rule_files = [str(f) for f in review.get("rule_files_changed") or []]
    if rule_files:
        lines.append(
            "!! RULE FILES CHANGED by this change: %s. The change alters the rules it is "
            "graded by; review those edits first." % ", ".join(rule_files)
        )
    current = review.get("current_graph_snapshot_id")
    if current and review.get("graph_snapshot_id") and current != review["graph_snapshot_id"]:
        lines.append(
            "The code graph was reloaded since this review (reviewed %s): it describes an "
            "earlier graph. Run the review again." % (review.get("reviewed_at") or "earlier")
        )
    failures = [f for f in verdict.get("failures") or [] if isinstance(f, dict)]
    if failures:
        lines.append("Failed the policy:")
        lines.extend("  - " + _finding_line(f) for f in failures[:_MAX_LISTED])
    new = [f for f in (verdict.get("diff") or {}).get("findings_new") or [] if isinstance(f, dict)]
    if new:
        by_explainer: dict = {}
        for finding in new:
            name = str(finding.get("source") or "?")
            by_explainer[name] = by_explainer.get(name, 0) + 1
        lines.append("New findings by explainer: " + _counts(by_explainer))
        lines.extend("  - " + _finding_line(f) for f in new[:_MAX_LISTED])
        if len(new) > _MAX_LISTED:
            lines.append("  (%d more; --json prints all)" % (len(new) - _MAX_LISTED))
    else:
        lines.append("New findings: none")
    below_floor = below_floor_line(verdict)
    if below_floor:
        lines.append(below_floor)
    added = _counts(verdict.get("edge_kinds_added"))
    removed = _counts(verdict.get("edge_kinds_removed"))
    lines.append(
        "Edges added: %s%s; removed: %s%s"
        % (
            verdict.get("edges_added", 0),
            " (" + added + ")" if added else "",
            verdict.get("edges_removed", 0),
            " (" + removed + ")" if removed else "",
        )
    )
    lines.append(
        "Facts added: %s; removed: %s; changed: %s"
        % (
            verdict.get("facts_added", 0),
            verdict.get("facts_removed", 0),
            verdict.get("facts_changed", 0),
        )
    )
    scope = _scope_lines(verdict)
    lines.append(
        "Outside the expected scope: " + ("; ".join(scope) if scope else "nothing reported")
    )
    return "\n".join(lines)


# -- Where to look ---------------------------------------------------------------


def _reason(answer: dict) -> str:
    return " ".join(str(answer.get("message") or answer.get("code") or "failed").split())[:160]


def _refresh_graph(state: dict, rt: dict, timeout: float, opener=None) -> str:
    """Load a working tree edited since the last index, so the review, ``locate``
    and ``impact`` read the code under review; "" or why the graph may lag."""
    kwargs = {"run_in_background": False, "timeout": timeout}
    if opener is not None:
        kwargs["opener"] = opener
    outcome = _code_graph.reingest_if_changed(
        state["repo_root"], rt["service_url"], rt.get("api_key", ""), **kwargs
    )
    if outcome.get("changed") is False or outcome.get("submitted"):
        return ""
    return str(outcome.get("error") or "the re-index was not started")[:160]


def _finding_symbols(finding: dict) -> list:
    """``(symbol, file, line)`` of each evidence of a finding that names a symbol."""
    found = []
    for evidence in finding.get("evidence") or []:
        if isinstance(evidence, dict) and evidence.get("symbol") and evidence.get("file"):
            found.append((str(evidence["symbol"]), str(evidence["file"]), evidence.get("line")))
    return found


def where_to_look(
    state: dict,
    rt: dict,
    review: dict,
    verdict: dict,
    *,
    target: str = "",
    graph_lag: str = "",
    budget: float = WHERE_BUDGET_SECONDS,
    opener=None,
) -> dict:
    """The pieces of the "Where to look" section, as data.

    ``changed_symbols`` are the symbols the diff since the merge-base touches,
    each with ``dependents`` / ``direct_dependents`` (``impact``, asked for the
    ``IMPACT_SYMBOLS`` largest) and ``fix_commits`` (``touched`` over the
    history before the merge-base, commits whose subject matches
    ``FIX_SUBJECT``). ``places`` are at most ``MAX_PLACES`` symbols with a
    reason to read them: something depends on them, a fix changed them before,
    a new finding names them, or the change removed them. ``unavailable`` has
    one line per part that failed or ran out of time; the rest is still there.
    ``graph_lag`` is ``run_review``'s: why the graph could not be brought up to
    the working tree before the review.
    """
    deadline = time.monotonic() + budget
    root = str(state["repo_root"])
    dataset = state["dataset"]
    base_commit = str(review.get("base_commit") or "")
    diff = verdict.get("diff") if isinstance(verdict.get("diff"), dict) else {}
    where = {
        "base_commit": base_commit,
        "changed_symbols": [],
        "impact_asked": 0,
        "impact_left_out": 0,
        "new_findings": [
            f
            for f in list(verdict.get("failures") or []) + list(diff.get("findings_new") or [])
            if isinstance(f, dict)
        ],
        "resolved_findings": [
            f for f in diff.get("findings_resolved") or [] if isinstance(f, dict)
        ],
        "removed_symbols": [
            {
                "name": str(f.get("name") or ""),
                "file": str(f.get("file") or ""),
                "line": f.get("line"),
            }
            for f in diff.get("facts_removed") or []
            if isinstance(f, dict) and f.get("kind") == "symbol" and f.get("name")
        ],
        "outside_scope": _scope_lines(verdict) if target else [],
        "target": target,
        "places": [],
        "more_places": 0,
        "unavailable": [],
    }

    def call(analysis: str, params: dict) -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "ok": False,
                "code": "timeout",
                "message": "no time left in the review's budget",
            }
        return _code_analysis.analyze(
            rt, FEATURE, analysis, params, dataset, timeout=remaining, opener=opener
        )

    symbols: list = []
    change = _code_graph._run_git(["diff", base_commit], root) if base_commit else ""
    if not change.strip():
        where["unavailable"].append(
            "changed symbols: git shows no diff against the merge-base %s"
            % (base_commit[:10] or "?")
        )
    else:
        if graph_lag:
            where["unavailable"].append(
                "code graph refresh: %s; symbols are read from the graph as last indexed"
                % graph_lag
            )
        answer = call("locate", {"diff": change})
        if not answer["ok"]:
            where["unavailable"].append("changed symbols: " + _reason(answer))
        else:
            located = (answer["result"].get("output") or {}).get("diff") or {}
            seen = set()
            for entry in located.get("files") or []:
                for symbol in (entry.get("symbols") or []) if isinstance(entry, dict) else []:
                    if not isinstance(symbol, dict) or not symbol.get("name"):
                        continue
                    if symbol["name"] in seen:
                        continue
                    seen.add(symbol["name"])
                    symbols.append(
                        {
                            "id": str(symbol.get("id") or ""),
                            "name": str(symbol["name"]),
                            "kind": str(symbol.get("symbol_kind") or ""),
                            "file": str(symbol.get("file") or ""),
                            "line": symbol.get("line"),
                            "end_line": symbol.get("end_line"),
                            "lines": int(symbol.get("lines") or 0),
                        }
                    )
    where["changed_symbols"] = symbols

    if symbols:
        asked = sorted(symbols, key=lambda s: -s["lines"])[:IMPACT_SYMBOLS]
        where["impact_asked"] = len(asked)
        where["impact_left_out"] = len(symbols) - len(asked)
        failed = []
        for symbol in asked:
            answer = call("impact", {"target": symbol["name"]})
            if not answer["ok"]:
                failed.append((symbol["name"], _reason(answer)))
                continue
            by_depth = (answer["result"].get("output") or {}).get("by_depth") or {}
            by_depth = by_depth if isinstance(by_depth, dict) else {}
            symbol["dependents"] = sum(len(v) for v in by_depth.values() if isinstance(v, list))
            symbol["direct_dependents"] = len(by_depth.get("1") or [])
        if failed:
            where["unavailable"].append(
                "impact of %s: %s" % (", ".join(_short_name(n) for n, _ in failed), failed[0][1])
            )
        if base_commit:
            answer = call("touched", {"rev": base_commit, "subject": FIX_SUBJECT, "top": 0})
            if not answer["ok"]:
                where["unavailable"].append("fix history: " + _reason(answer))
            else:
                ranking = (answer["result"].get("output") or {}).get("ranking") or []
                counts = {
                    str(r.get("id") or r.get("name")): int(r.get("count") or 0)
                    for r in ranking
                    if isinstance(r, dict)
                }
                for symbol in symbols:
                    count = counts.get(symbol["id"]) or counts.get(symbol["name"]) or 0
                    if count:
                        symbol["fix_commits"] = count

    where["places"], where["more_places"] = _places(where)
    return where


def _short_name(name: str) -> str:
    return str(name).rsplit("/", 1)[-1]


def _places(where: dict) -> tuple:
    """``(places, how many more had a reason)``: most depended-on first, then by
    earlier fixes, then findings; removed symbols after the changed ones."""
    by_name: dict = {}

    def place(name: str, file: str, line) -> dict:
        return by_name.setdefault(
            name,
            {"symbol": name, "file": file, "line": line, "reasons": [], "rank": (0, 0, 0, 0)},
        )

    for symbol in where["changed_symbols"]:
        dependents = int(symbol.get("dependents") or 0)
        fixes = int(symbol.get("fix_commits") or 0)
        if not dependents and not fixes:
            continue
        entry = place(symbol["name"], symbol["file"], symbol.get("line"))
        if dependents:
            entry["reasons"].append(
                "%d symbol(s) depend on it (%d directly)"
                % (dependents, int(symbol.get("direct_dependents") or 0))
            )
        if fixes:
            entry["reasons"].append("changed by %d fix commit(s) before" % fixes)
        entry["rank"] = (dependents, int(symbol.get("direct_dependents") or 0), fixes, 0)
    for finding in where["new_findings"]:
        for name, file, line in _finding_symbols(finding):
            entry = place(name, file, line)
            reason = "new finding [%s]" % (finding.get("source") or "?")
            if reason not in entry["reasons"]:
                entry["reasons"].append(reason)
                entry["rank"] = (*entry["rank"][:3], entry["rank"][3] + 1)
    for removed in where["removed_symbols"]:
        entry = place(removed["name"], removed["file"], removed.get("line"))
        entry["reasons"].append("removed by this change; check what still refers to it")
    ordered = sorted(by_name.values(), key=lambda entry: tuple(-n for n in entry["rank"]))
    places = [{k: v for k, v in entry.items() if k != "rank"} for entry in ordered]
    return places[:MAX_PLACES], max(0, len(places) - MAX_PLACES)


def place_lines(where: dict) -> list:
    """One line per place: ``symbol (file:line): reasons``."""
    lines = []
    for place in where.get("places") or []:
        location = str(place.get("file") or "")
        if location and place.get("line"):
            location += ":%s" % place["line"]
        line = "%s%s: %s" % (
            place.get("symbol") or "?",
            " (%s)" % location if location else "",
            "; ".join(place.get("reasons") or []),
        )
        lines.append(line[:_PLACE_LINE_CHARS])
    return lines


def where_lines(where: dict) -> list:
    """The "Where to look" section of the report."""
    lines = ["Where to look:"]
    places = place_lines(where)
    lines.extend("  %d. %s" % (index + 1, line) for index, line in enumerate(places))
    if not places:
        changed = len(where.get("changed_symbols") or [])
        lines.append(
            "  nothing stands out: %d changed symbol(s), none with dependents, earlier fixes, "
            "a new finding or removed" % changed
        )
    if where.get("more_places"):
        lines.append(
            "  (%d more place(s) had a reason; --json lists all the pieces)" % where["more_places"]
        )
    if where.get("impact_left_out"):
        lines.append(
            "  (impact was asked for the %d largest of %d changed symbols)"
            % (where["impact_asked"], where["impact_asked"] + where["impact_left_out"])
        )
    resolved = where.get("resolved_findings") or []
    if resolved:
        lines.append("Resolved findings: %d" % len(resolved))
        lines.extend("  - " + _finding_line(f) for f in resolved[:_MAX_LISTED])
    removed = where.get("removed_symbols") or []
    if removed:
        lines.append(
            "Removed symbols: "
            + ", ".join(r["name"] for r in removed[:_MAX_LISTED])
            + (" (+%d more)" % (len(removed) - _MAX_LISTED) if len(removed) > _MAX_LISTED else "")
        )
    if where.get("target"):
        scope = where.get("outside_scope") or []
        lines.append(
            "Reached outside %s: %s" % (where["target"], "; ".join(scope) if scope else "nothing")
        )
    lines.extend("Not available: " + line for line in where.get("unavailable") or [])
    return lines


def note_where(repo_root: str, review: dict, where: dict) -> None:
    """Leave an automatic review's places for the next prompt's notice."""
    marker = _code_analysis.read_json(_marker_path(repo_root))
    marker.update(where=place_lines(where), where_for=str(review.get("reviewed_at") or ""))
    _code_analysis.write_json(_marker_path(repo_root), marker)


# -- Prompt hook -----------------------------------------------------------------


def _notice_text(review: dict, where: list = ()) -> str:
    """The block for one stored verdict. ``where`` are the places an automatic
    review left for it: they are shown even when the verdict is clean."""
    verdict = review.get("verdict") if isinstance(review.get("verdict"), dict) else {}
    regression = is_regression(verdict, review.get("exit_code"))
    rule_files = [str(f) for f in review.get("rule_files_changed") or []]
    where = [str(line) for line in where or [] if str(line).strip()][:MAX_PLACES]
    if not regression and not rule_files and not where:
        return ""
    lines = [
        "Cognee code review of %s against %s (stored verdict, reviewed %s): %s"
        % (
            review.get("repo") or "this repository",
            review.get("base") or "?",
            str(review.get("reviewed_at") or "?")[:19],
            "REGRESSION" if regression else str(verdict.get("status") or "no regression"),
        )
    ]
    for failure in [f for f in verdict.get("failures") or [] if isinstance(f, dict)][:4]:
        lines.append("- " + _finding_line(failure))
    lines.extend("- " + line for line in _scope_lines(verdict)[:2])
    if rule_files:
        lines.append("Rule files changed by the change itself: " + ", ".join(rule_files[:6]))
    current = review.get("current_graph_snapshot_id")
    if current and review.get("graph_snapshot_id") and current != review["graph_snapshot_id"]:
        lines.append("The code graph was reloaded since this review; run it again.")
    lines.append("Full report: cognee-review.sh --stored (the cognee-architecture skill).")
    text = "\n".join(lines)[:NOTICE_MAX_CHARS]
    if where:
        text += "\nWhere to look:\n" + "\n".join("- " + line for line in where)
    return text


def prompt_notice(cwd: str, rt: dict, *, timeout=NOTICE_TIMEOUT_SECONDS, opener=None) -> tuple:
    """``(text, mark_shown)`` for the prompt hook; ``("", None)`` when there is
    nothing to show. ``mark_shown`` is called once the text was printed.

    ``rt`` is a callable returning the runtime, resolved only when a review is
    pending. The read is bounded by ``timeout`` in elapsed time.
    """
    state = _code_graph.find_indexed_repo(cwd)
    if state.get("spec_kind") != "path" or not state.get("dataset"):
        return "", None
    path = _marker_path(state["repo_root"])
    marker = _code_analysis.read_json(path)
    if not marker.get("pending"):
        return "", None
    runtime = rt() if callable(rt) else rt

    def settle(**changes) -> None:
        marker.update(changes)
        _code_analysis.write_json(path, marker)

    if not _code_analysis.operation_known(runtime, "review"):
        settle(pending="")
        return "", None
    from _deadlines import bounded_call

    try:
        answer = bounded_call(
            lambda: _read_review(state, runtime, timeout=timeout, opener=opener), timeout
        )
    except TimeoutError:
        answer = {"ok": False, "code": "timeout"}
    if not answer["ok"]:
        attempts = int(marker.get("attempts") or 0) + 1
        gone = answer["code"] == "unknown_operation" or attempts >= NOTICE_MAX_ATTEMPTS
        settle(attempts=attempts, pending="" if gone else marker["pending"])
        return "", None
    review = answer["review"] or {}
    reviewed_at = str(review.get("reviewed_at") or "")
    where = marker.get("where") if marker.get("where_for") == reviewed_at else []
    text = ""
    if reviewed_at and reviewed_at != marker.get("shown"):
        text = _notice_text(review, where if isinstance(where, list) else [])
    if not text:
        settle(pending="", shown=reviewed_at or marker.get("shown") or "", attempts=0, where=[])
        return "", None

    def mark_shown() -> None:
        settle(pending="", shown=reviewed_at, shown_at=time.time(), attempts=0, where=[])

    return text, mark_shown
