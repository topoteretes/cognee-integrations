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

Idempotent: our entries are recognised by their command — a ``run-cursor-hook``
launcher (this checkout's or a moved one's) running one of the plugin's hook
scripts — and replaced on every run; nothing else is touched. A ``hooks.json``
whose shape it does not understand is left alone with an error, never rewritten.
The file is replaced atomically, keeps its permissions, and a symlinked one
(dotfiles) is written through to its target. Cursor watches ``hooks.json`` and
reloads it on save.

The commands hold this checkout's absolute path: moving or deleting the checkout
breaks them until the installer is run again from the new location. For the same
reason a project-level file is only useful on machines that have the checkout at
that path, so don't commit it for other people or cloud agents.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cursor_hook import HOOK_TABLE  # noqa: E402

LAUNCHER_NAMES = frozenset({"run-cursor-hook", "run-cursor-hook.cmd"})


def launcher_path() -> Path:
    name = "run-cursor-hook.cmd" if os.name == "nt" else "run-cursor-hook"
    return Path(__file__).resolve().with_name(name)


def render_command(script: str, flags: tuple[str, ...]) -> str:
    launcher = str(launcher_path())
    if os.name == "nt":
        # Always quoted: cmd treats & ( ) ^ in a bare path as syntax.
        quoted = f'"{launcher}"'
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


class UnsupportedHooksFile(ValueError):
    """``hooks.json`` has a shape this installer will not rewrite."""


_SCRIPTS = frozenset(script for entries in HOOK_TABLE.values() for script, _, _ in entries)


def _command_tokens(command: str) -> tuple[str, list[str]]:
    """``(launcher, rest)`` of a command as :func:`render_command` writes it, on
    either OS (Windows paths keep their backslashes, so no POSIX shlex there)."""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        if end < 0:
            return "", []
        return command[1:end], command[end + 1 :].split()
    if command.startswith("'"):
        try:
            parts = shlex.split(command)
        except ValueError:
            return "", []
        return (parts[0], parts[1:]) if parts else ("", [])
    parts = command.split()
    return (parts[0], parts[1:]) if parts else ("", [])


def _is_ours(entry: object) -> bool:
    """A ``run-cursor-hook`` launcher running one of our hook scripts — not any
    command that merely contains the launcher's name."""
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
        return False
    launcher, rest = _command_tokens(entry["command"])
    name = re.split(r"[\\/]", launcher)[-1]
    return name in LAUNCHER_NAMES and bool(rest) and rest[0] in _SCRIPTS


def merge(existing: dict, *, uninstall: bool = False) -> dict:
    """Return ``existing`` with our entries replaced (or removed).

    Raises :class:`UnsupportedHooksFile` instead of guessing when ``hooks`` is not
    an object or an event's value is not a list: rewriting either would lose the
    user's own hooks.
    """
    result = dict(existing) if isinstance(existing, dict) else {}
    result.setdefault("version", 1)
    hooks = result.get("hooks", {})
    if not isinstance(hooks, dict):
        raise UnsupportedHooksFile('"hooks" is not an object')
    hooks = dict(hooks)
    for event, entries in list(hooks.items()):
        if not isinstance(entries, list):
            raise UnsupportedHooksFile(f'"hooks.{event}" is not a list')
        kept = [entry for entry in entries if not _is_ours(entry)]
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if not uninstall:
        for event, entries in our_entries().items():
            hooks[event] = list(hooks.get(event, [])) + entries
    result["hooks"] = hooks
    return result


def write(path: Path, text: str) -> None:
    """Replace ``path`` atomically: unique temp file, same permissions, and a
    symlink written through to its target instead of replaced by a file."""
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, stat.S_IMODE(target.stat().st_mode))
        except FileNotFoundError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    try:
        merged = merge(load(path), uninstall=args.uninstall)
    except UnsupportedHooksFile as exc:
        raise SystemExit(f"{path}: {exc}; refusing to rewrite it, fix the file and rerun")
    rendered = json.dumps(merged, indent=2) + "\n"
    if args.show:
        print(rendered, end="")
        return 0
    write(path, rendered)
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
