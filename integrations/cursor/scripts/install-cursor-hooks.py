#!/usr/bin/env python3
"""Register the Cognee hooks in a Cursor ``hooks.json`` (user or project level).

Cursor loads hooks from ``~/.cursor/hooks.json`` (user), ``<project>/.cursor/
hooks.json`` (project; also used by cloud agents) and from installed plugins.
This installer is for the first two: it writes absolute-path commands for this
checkout into an existing ``hooks.json`` without touching anyone else's entries.

    python3 scripts/install-cursor-hooks.py                 # ~/.cursor/hooks.json
    python3 scripts/install-cursor-hooks.py --project .     # ./.cursor/hooks.json
    python3 scripts/install-cursor-hooks.py --uninstall     # remove our entries
    python3 scripts/install-cursor-hooks.py --print         # show, write nothing

Idempotent: our entries are recognised by the ``run-cursor-hook`` launcher in
their command and replaced on every run. Cursor watches ``hooks.json`` and
reloads it on save.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cursor_hook import HOOK_TABLE  # noqa: E402

MARKER = "run-cursor-hook"


def launcher_path() -> Path:
    name = "run-cursor-hook.cmd" if os.name == "nt" else "run-cursor-hook"
    return Path(__file__).resolve().with_name(name)


def render_command(script: str, flags: tuple[str, ...]) -> str:
    launcher = str(launcher_path())
    if os.name == "nt":
        quoted = f'"{launcher}"' if " " in launcher else launcher
    else:
        quoted = shlex.quote(launcher)
    return " ".join([quoted, script, *flags])


def our_entries() -> dict[str, list[dict]]:
    return {
        event: [
            {"command": render_command(script, flags), "timeout": timeout}
            for script, flags, timeout in entries
        ]
        for event, entries in HOOK_TABLE.items()
    }


def _is_ours(entry: object) -> bool:
    return isinstance(entry, dict) and MARKER in str(entry.get("command", ""))


def merge(existing: dict, *, uninstall: bool = False) -> dict:
    """Return ``existing`` with our entries replaced (or removed)."""
    result = dict(existing) if isinstance(existing, dict) else {}
    result.setdefault("version", 1)
    hooks = result.get("hooks")
    hooks = dict(hooks) if isinstance(hooks, dict) else {}
    for event, entries in list(hooks.items()):
        kept = (
            [entry for entry in entries if not _is_ours(entry)]
            if isinstance(entries, list)
            else entries
        )
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if not uninstall:
        for event, entries in our_entries().items():
            hooks.setdefault(event, [])
            hooks[event] = list(hooks[event]) + entries
    result["hooks"] = hooks
    return result


def target_path(project: str | None) -> Path:
    if project:
        return Path(project).expanduser().resolve() / ".cursor" / "hooks.json"
    return Path.home() / ".cursor" / "hooks.json"


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read {path}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"{path} is not a JSON object; refusing to overwrite it")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--project",
        metavar="DIR",
        help="write <DIR>/.cursor/hooks.json instead of ~/.cursor/hooks.json",
    )
    parser.add_argument("--uninstall", action="store_true", help="remove the Cognee entries")
    parser.add_argument(
        "--print", dest="show", action="store_true", help="print the resulting file, do not write"
    )
    args = parser.parse_args(argv)

    path = target_path(args.project)
    merged = merge(load(path), uninstall=args.uninstall)
    rendered = json.dumps(merged, indent=2) + "\n"
    if args.show:
        print(rendered, end="")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(rendered, encoding="utf-8")
    os.replace(tmp, path)
    verb = "removed from" if args.uninstall else "written to"
    print(f"Cognee hooks {verb} {path}")
    if not args.uninstall:
        print(
            "Cursor reloads hooks.json automatically; "
            "open a new agent conversation to start capturing."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
