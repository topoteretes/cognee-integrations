#!/usr/bin/env python3
"""Review what the working tree did to the architecture since a base ref.

Usage:
  cognee-review.py [<base-ref>] [--fail-on <explainers>] [--min-confidence <x>]
                   [--max-spillover <n>] [--target <symbol-or-package>]
                   [--expected <packages>] [--json]
  cognee-review.py --stored [--json]

<base-ref>:      what to compare against (a branch, tag or commit). Default: the
                 remote default branch (refs/remotes/origin/HEAD); required
                 when git names none.
--fail-on:       comma-separated enola explainers whose new findings count as a
                 regression (cycles, layers, constraints, dead-code, ...).
                 Without --fail-on / --max-spillover everything is reported and
                 nothing is a regression.
--min-confidence: how sure enola must be of a --fail-on finding for it to
                 count, above 0 up to 1. enola's default is 1: only certain
                 findings count, so a heuristic one (dead-code is 0.8) is
                 reported but is not a regression unless this is lowered.
--max-spillover: regression when more than <n> packages are reached outside
                 the scope given with --target / --expected.
--target:        the symbol, type or package you meant to change.
--expected:      comma-separated packages you expected to touch as well.
--stored:        print the verdict the server stored for this repository,
                 without running a review (says when the graph was reloaded
                 since).
--json:          print the server's reply instead of the report, with the
                 pieces of the "Where to look" section under `where_to_look`.
--no-where:      skip the "Where to look" section, its extra analyses and the
                 re-index of an edited working tree before the review.

The Cognee server runs enola on the repository this checkout was indexed from,
so the review needs a local server with the code analysis API and a repository
indexed by path (cognee-index-repo.sh <path>). The server stores the verdict;
a regression or changed rule files are then shown once at the next prompt.

After the verdict the report says where a reviewer should look: at most five
symbols the change touched, each with its file and line and the reason (how
much depends on it, earlier fix commits that changed it, a new finding, or
that the change removed it), then resolved findings, removed symbols and, with
--target, what the change reached outside it. Those come from the server's
`locate`, `impact` and `touched` analyses, asked one after another within 60
seconds; one that fails or runs out of time costs its part and a line saying
so, never the review. `impact` is asked for the five largest changed symbols.

Exit status: 0 reviewed (clean), 1 regression, 2 the review could not run,
3 the base is not comparable.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _code_analysis
import _code_review

_VALUE_FLAGS = {
    "--fail-on": "fail_on",
    "--min-confidence": "min_confidence",
    "--max-spillover": "max_spillover",
    "--target": "target",
    "--expected": "expected",
}


def _parse(argv: list) -> tuple:
    base, params, flags = "", {}, set()
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in _VALUE_FLAGS:
            index += 1
            value = argv[index] if index < len(argv) else ""
            name = _VALUE_FLAGS[arg]
            if name in ("fail_on", "expected"):
                params[name] = [item.strip() for item in value.split(",") if item.strip()]
            elif name == "max_spillover":
                params[name] = int(value)
            elif name == "min_confidence":
                params[name] = float(value)
            else:
                params[name] = value
        elif arg in ("--stored", "--json", "--no-where"):
            flags.add(arg)
        elif arg in ("-h", "--help"):
            flags.add("--help")
        elif not arg.startswith("-") and not base:
            base = arg
        else:
            raise ValueError("unknown argument: " + arg)
        index += 1
    return base, params, flags


def main(argv: list) -> int:
    try:
        base, params, flags = _parse(argv)
    except ValueError as exc:
        print("Cognee review: %s (see --help)" % exc, file=sys.stderr)
        return 2
    if "--help" in flags:
        print(__doc__.strip())
        return 0
    rt = _code_analysis.runtime()
    cwd = os.getcwd()
    if "--stored" in flags:
        answer = _code_review.stored_review(cwd, rt)
        if not answer["ok"]:
            print(answer["message"])
            return 2
        review = answer["review"]
        if not review:
            print("Cognee review: no stored verdict for this repository. Run a review first.")
            return 0
        if "--json" in flags:
            print(json.dumps(review))
        else:
            verdict = review.get("verdict") if isinstance(review.get("verdict"), dict) else {}
            print(
                _code_review.report(
                    review, verdict, review.get("exit_code"), str(review.get("enola_version") or "")
                )
            )
        return 0
    answer = _code_review.run_review(cwd, base, params, rt, refresh="--no-where" not in flags)
    if not answer["ok"]:
        print(answer["message"])
        return 2
    result = answer["result"]
    verdict = result.get("output") if isinstance(result.get("output"), dict) else {}
    where = None
    if "--no-where" not in flags and result.get("exit_code") != 3:
        try:
            where = _code_review.where_to_look(
                answer["state"],
                rt,
                result.get("review") or {},
                verdict,
                target=str(params.get("target") or ""),
                graph_lag=answer.get("graph_lag", ""),
            )
        except Exception as exc:  # the verdict is printed whatever happens here
            where = {"places": [], "unavailable": ["where to look: %s" % str(exc)[:160]]}
    if "--json" in flags:
        print(json.dumps({**result, "where_to_look": where} if where is not None else result))
    else:
        print(
            _code_review.report(
                result.get("review") or {},
                verdict,
                result.get("exit_code"),
                str(result.get("enola_version") or ""),
            )
        )
        if where is not None:
            print("\n".join(_code_review.where_lines(where)))
    exit_code = result.get("exit_code")
    return exit_code if exit_code in (1, 3) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
