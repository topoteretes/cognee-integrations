#!/usr/bin/env python3
"""Cognee plugin health check — run to diagnose plugin issues.

Usage: python doctor.py [--json]
Exit 0 = all checks passed; 1 = issues found.
"""

import json
import sys
import time
from pathlib import Path

_ROOT = Path.home() / ".cognee-plugin"
_PLUGIN = _ROOT / "claude-code"
_BREAKER = _ROOT / "recall-breaker.json"
_RECALL = _PLUGIN / "last_recall.json"
_COUNTER = _PLUGIN / "counter.json"


def _slurp(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def _check_server(base_url: str) -> dict:
    if not base_url:
        return {"status": "local_sdk"}
    import urllib.error
    import urllib.request

    url = base_url.rstrip("/") + "/health"
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return {"status": "ok", "latency_ms": int((time.monotonic() - t0) * 1000)}
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:120]}


def _pkg_version(name: str) -> str:
    try:
        import importlib.metadata

        return importlib.metadata.version(name)
    except Exception:
        return "?"


def build_report() -> dict:
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from _plugin_common import resolve_runtime_mode

        rt = resolve_runtime_mode()
    except Exception:
        rt = {
            "mode": "unknown",
            "base_url": "",
            "api_key_present": False,
            "url_source": "?",
            "key_source": "?",
        }

    breaker = _slurp(_BREAKER)
    recall = _slurp(_RECALL)
    counter = _slurp(_COUNTER)
    base_url = rt.get("base_url", "")

    cooldown = float(breaker.get("cooldown_until") or 0)
    breaker_open = cooldown > time.time()

    server = _check_server(base_url)

    issues = []
    if server.get("status") == "error":
        issues.append(f"server unreachable at {base_url}")
    if breaker_open:
        issues.append(f"recall breaker open (cooldown until {cooldown:.0f})")

    return {
        "mode": rt.get("mode", "unknown"),
        "base_url": base_url or "(local)",
        "api_key_present": rt.get("api_key_present", False),
        "server": server,
        "recall_breaker": {"open": breaker_open, "failures": breaker.get("failure_count", 0)},
        "last_recall": {"ts": recall.get("ts", "never"), "hits": recall.get("hits", {})},
        "turn_counter": counter.get("total", 0),
        "versions": {
            "cognee": _pkg_version("cognee"),
            "plugin": _pkg_version("cognee-plugin"),
        },
        "issues": issues,
    }


def _print_human(r: dict) -> None:
    ok, warn, fail, info = "✓", "⚠", "✗", "·"

    def row(sym: str, label: str, val: str) -> None:
        print(f"  {sym}  {label:<24}{val}")

    print("\ncognee-plugin doctor\n" + "─" * 42)
    row(info, "Mode", r["mode"])
    row(info, "Backend URL", r["base_url"])
    row(info if r["api_key_present"] else warn, "API key", "present" if r["api_key_present"] else "not set")

    s = r["server"]
    if s.get("status") == "local_sdk":
        row(info, "Server", "local SDK (no HTTP check)")
    elif s.get("status") == "ok":
        row(ok, "Server", f"reachable ({s.get('latency_ms', '?')} ms)")
    else:
        row(fail, "Server", f"unreachable — {s.get('error', '')}")

    br = r["recall_breaker"]
    row(
        warn if br["open"] else ok,
        "Recall breaker",
        f"OPEN ({br['failures']} failures)" if br["open"] else "closed",
    )

    lr = r["last_recall"]
    row(info, "Last recall", lr["ts"])
    row(info, "Turn counter", str(r["turn_counter"]))

    ver = r["versions"]
    row(info, "cognee", ver["cognee"])
    row(info, "plugin", ver["plugin"])

    print()
    if r["issues"]:
        for issue in r["issues"]:
            print(f"  {fail}  {issue}")
        print()
    else:
        print(f"  {ok}  All checks passed\n")


def main() -> int:
    report = build_report()
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    return 1 if report["issues"] else 0


if __name__ == "__main__":
    sys.exit(main())
