#!/usr/bin/env python3
"""Architecture review of a change (enola ``check``, run by the Cognee server).

Three parts, shared by ``cognee-review.py`` and the prompt hook:

* ``run_review`` asks the server to grade the working tree of the checkout's
  path-indexed repository against a base ref. The server stores the verdict on
  the repository's code graph.
* ``report`` renders a verdict as a short text report.
* ``prompt_notice`` is the prompt hook's part. A review run by the script leaves
  a marker for its repository; while the marker is pending, the next prompt
  reads the stored verdict with the ``review`` CODE operation and, when it
  reports a regression or changed rule files, returns a short block once for
  that verdict. Without a marker it costs one file read and no request.

Nothing here runs enola or a review on its own.
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


def default_base(repo_root: str) -> str:
    """The remote default branch (``origin/main``), or "" when git names none."""
    return _code_graph._run_git(
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], repo_root
    ).strip()


def _marker_path(repo_root: str):
    return _code_analysis.repo_state_path(repo_root)


def run_review(cwd: str, base: str, params: dict, rt: dict, *, opener=None) -> dict:
    """``{"ok": True, "result", "state"}`` or ``{"ok": False, "message", "code"}``."""
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
    answer = _code_analysis.analyze(
        rt, FEATURE, "check", {"base": base, **params}, state["dataset"], opener=opener
    )
    if not answer["ok"]:
        return answer
    review = answer["result"].get("review") or {}
    if review.get("reviewed_at"):
        marker = _code_analysis.read_json(_marker_path(state["repo_root"]))
        marker.update(pending=str(review["reviewed_at"]), attempts=0)
        _code_analysis.write_json(_marker_path(state["repo_root"]), marker)
    return {"ok": True, "result": answer["result"], "state": state}


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
    return "[%s] %s%s" % (finding.get("source") or "?", finding.get("title") or "", where)


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


# -- Prompt hook -----------------------------------------------------------------


def _notice_text(review: dict) -> str:
    verdict = review.get("verdict") if isinstance(review.get("verdict"), dict) else {}
    regression = is_regression(verdict, review.get("exit_code"))
    rule_files = [str(f) for f in review.get("rule_files_changed") or []]
    if not regression and not rule_files:
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
    return "\n".join(lines)[:NOTICE_MAX_CHARS]


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
    text = _notice_text(review) if reviewed_at and reviewed_at != marker.get("shown") else ""
    if not text:
        settle(pending="", shown=reviewed_at or marker.get("shown") or "", attempts=0)
        return "", None

    def mark_shown() -> None:
        settle(pending="", shown=reviewed_at, shown_at=time.time(), attempts=0)

    return text, mark_shown
