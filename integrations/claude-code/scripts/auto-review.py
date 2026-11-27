#!/usr/bin/env python3
"""Automatic code review after a commit, a push or a new pull request.

PostToolUse hook for ``Bash`` (asynchronous). ``_review_gate.decide`` reads the
call and local state only; when it says no, this hook makes no request and
writes one ``auto_review_skipped`` line to ``hook.log``. When it says yes, a
detached copy of this script (``--run``) runs the review the way
``cognee-review.sh`` does (the server's ``check``, then the "Where to look"
analyses, one call after another) and leaves the places for the next prompt's
review notice. The hook itself returns at once.

``COGNEE_AUTO_REVIEW=false`` turns it off. Nothing here can fail the agent's
turn: every failure is one line in ``hook.log``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import hook_log, is_observer_child, quiet_hook_output

_RUN_ARG = "--run"
#: Seconds the server's ``check`` may take in an automatic review.
CHECK_TIMEOUT_SECONDS = 300.0


def _runtime() -> dict:
    from _plugin_common import _api_key, _local_api_url

    return {"service_url": _local_api_url(), "api_key": _api_key()}


def _spawn_process(argv: list) -> None:
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
        start_new_session=True,
        close_fds=True,
    )


def _spawn(decision: dict) -> bool:
    try:
        _spawn_process(
            [
                sys.executable,
                os.path.abspath(__file__),
                _RUN_ARG,
                decision["repo_root"],
                decision["base"],
                decision["head"],
                decision["trigger"],
            ]
        )
        return True
    except Exception as exc:
        hook_log("auto_review_spawn_failed", {"error": str(exc)[:200]})
        return False


class _CountingOpener:
    """The plugin's opener, counting the requests of one review."""

    def __init__(self):
        from _recall_http import _HTTPS_OPENER

        self._open = _HTTPS_OPENER.open
        self.calls = 0

    def __call__(self, request, timeout=None):
        self.calls += 1
        return self._open(request, timeout=timeout)


def review(repo_root: str, base: str, head: str, trigger: str, *, opener=None) -> dict:
    """Run the review and leave its places for the next prompt. Returns the
    outcome that is logged: ``{"ok", "status", "places", "requests", "elapsed_ms"}``."""
    import _code_review
    import _review_gate

    started = time.monotonic()
    _review_gate.settle(repo_root, pid=os.getpid())
    opener = opener or _CountingOpener()
    rt = _runtime()
    outcome = {"trigger": trigger, "head": head[:12], "base": base}
    answer = _code_review.run_review(
        repo_root, base, {}, rt, opener=opener, timeout=CHECK_TIMEOUT_SECONDS, refresh=True
    )
    if not answer["ok"]:
        outcome.update(ok=False, code=str(answer.get("code") or "failed"))
    else:
        result = answer["result"]
        verdict = result.get("output") if isinstance(result.get("output"), dict) else {}
        stored = result.get("review") or {}
        places = 0
        if result.get("exit_code") != 3:
            where = _code_review.where_to_look(
                answer["state"],
                rt,
                stored,
                verdict,
                graph_lag=answer.get("graph_lag", ""),
                opener=opener,
            )
            _code_review.note_where(repo_root, stored, where)
            places = len(where["places"])
            outcome["unavailable"] = len(where["unavailable"])
        outcome.update(
            ok=True,
            status=str(verdict.get("status") or ""),
            exit_code=result.get("exit_code"),
            places=places,
        )
    outcome["requests"] = getattr(opener, "calls", None)
    outcome["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    _review_gate.settle(
        repo_root,
        status="done" if outcome["ok"] else "failed",
        finished_at=time.time(),
        elapsed_ms=outcome["elapsed_ms"],
        requests=outcome["requests"],
    )
    return outcome


def _run(argv: list) -> None:
    if len(argv) < 4:
        return
    repo_root, base, head, trigger = argv[:4]
    try:
        with quiet_hook_output("auto-review"):
            outcome = review(repo_root, base, head, trigger)
        if outcome["ok"]:
            hook_log("auto_review_finished", outcome)
        else:
            hook_log("auto_review_failed", outcome)
    except Exception as exc:
        hook_log("auto_review_failed", {"error": str(exc)[:200]})
        try:
            import _review_gate

            _review_gate.settle(repo_root, status="failed", finished_at=time.time())
        except Exception:
            pass


def main() -> None:
    if is_observer_child():
        return
    if _RUN_ARG in sys.argv:
        _run(sys.argv[sys.argv.index(_RUN_ARG) + 1 :])
        return
    payload_raw = sys.stdin.read()
    try:
        payload = json.loads(payload_raw) if payload_raw.strip() else None
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        return
    try:
        import _review_gate

        decision = _review_gate.decide(payload, service_url=lambda: _runtime()["service_url"])
        if not decision["run"]:
            hook_log(
                "auto_review_skipped",
                {"reason": decision["reason"], "trigger": decision.get("trigger", "")},
            )
        elif _spawn(decision):
            hook_log(
                "auto_review_started",
                {
                    "trigger": decision["trigger"],
                    "head": decision["head"][:12],
                    "base": decision["base"],
                },
            )
        else:
            _review_gate.settle(decision["repo_root"], status="failed", finished_at=time.time())
    except Exception as exc:
        hook_log("auto_review_gate_failed", {"error": str(exc)[:200]})


if __name__ == "__main__":
    main()
