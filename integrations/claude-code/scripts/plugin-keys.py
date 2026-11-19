#!/usr/bin/env python3
"""List and revoke the API keys of this plugin's Cognee agent, one per machine.

Every machine the plugin runs on holds its own key for the plugin's agent
(cognee#6113). The server caps the keys an agent may hold; when it is full of
keys still in use, a new machine is refused one (409) and runs on your
personal key until an old machine's key is revoked — which is what this
script is for.

Usage:
    plugin-keys.py list [--plugin KEY | --all] [--json]
    plugin-keys.py revoke <key_id> [--plugin KEY] [--force] [--json]

``list`` sorts by last use, most recent first; keys never used come last. The
key this machine holds is marked — revoke it only on purpose (``--force``):
the next session start then provisions a new one.

Authenticates as the user principal (the plugin's agent cannot manage its own
keys): COGNEE_PRINCIPAL_API_KEY, else COGNEE_API_KEY unless that is the cached
agent key, else the cached principal key (``~/.cognee-plugin/api_key.json``).

Exit codes:
    0  done                         3  no such key (already revoked, or another plugin's)
    1  request failed               4  refused: this machine's own key (pass --force)
    2  no principal key resolved    5  the server has no key routes for this plugin
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _plugin_common import (  # noqa: E402
    _AGENT_KEY_CACHE,
    PLUGIN_KEY,
    _json_http_request,
    _load_json_file,
    _local_api_url,
    _normalize_service_url,
    _row_str,
    clear_cached_agent_key,
    load_cached_agent_key,
    load_cached_api_key,
)

#: Plugins whose agents are listed under ``--all`` when the server's own
#: listing cannot be read (its status route predates the key routes, say).
FALLBACK_PLUGIN_KEYS = ("claude-code", "codex", "opencode", "openclaw", "mcp", "api", "desktop")


def principal_key(service_url: str) -> str:
    env_principal = str(os.environ.get("COGNEE_PRINCIPAL_API_KEY") or "").strip()
    if env_principal:
        return env_principal
    env_key = str(os.environ.get("COGNEE_API_KEY") or "").strip()
    if env_key and env_key != load_cached_agent_key(service_url):
        return env_key
    return load_cached_api_key(service_url)


def this_machine_key_id(service_url: str) -> str:
    """The server-side id of the key this machine holds, or ""."""
    record = _load_json_file(_AGENT_KEY_CACHE)
    if not isinstance(record, dict) or not load_cached_agent_key(service_url):
        return ""
    return str(record.get("key_id") or "")


def _fail(args, code: int, error: str, message: str) -> int:
    """Report a refusal or failure: JSON on stdout under --json, else text on stderr."""
    if args.json:
        print(json.dumps({"ok": False, "error": error, "message": message}))
    else:
        print(message, file=sys.stderr)
    return code


def _request(path: str, *, method: str, key: str, service_url: str):
    return _json_http_request(path, method=method, api_key=key, base_url=service_url, timeout=20.0)


def known_plugin_keys(key: str, service_url: str) -> list[str]:
    """Every plugin the server knows, from its status listing; a fallback list otherwise."""
    try:
        status = _request(
            "/api/v1/integrations/status", method="GET", key=key, service_url=service_url
        )
        rows = (status.get("plugins") if isinstance(status, dict) else None) or []
        keys = [
            _row_str(row, "key", "plugin_key", "pluginKey") for row in rows if isinstance(row, dict)
        ]
        keys = [k for k in keys if k]
        if keys:
            return keys
    except Exception:
        pass
    return list(FALLBACK_PLUGIN_KEYS)


def list_keys(plugin: str, *, key: str, service_url: str) -> list[dict]:
    """The keys of ``plugin``'s agent. Raises HTTPError; 404 means no key routes."""
    body = _request(
        f"/api/v1/integrations/plugins/{urllib.parse.quote(plugin, safe='')}/keys",
        method="GET",
        key=key,
        service_url=service_url,
    )
    rows = body.get("keys") if isinstance(body, dict) else None
    mine = this_machine_key_id(service_url) if plugin == PLUGIN_KEY else ""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key_id = _row_str(row, "id")
        out.append(
            {
                "plugin": plugin,
                "id": key_id,
                "name": _row_str(row, "name"),
                "label": _row_str(row, "label"),
                "created_at": _row_str(row, "created_at", "createdAt"),
                "last_used_at": _row_str(row, "last_used_at", "lastUsedAt"),
                "this_machine": bool(key_id) and key_id == mine,
            }
        )
    return out


def sort_by_last_use(rows: list[dict]) -> list[dict]:
    """Most recently used first; never-used keys last, newest of those first."""
    return sorted(
        rows,
        key=lambda r: (1, r["last_used_at"]) if r["last_used_at"] else (0, r["created_at"]),
        reverse=True,
    )


def _fmt(value: str) -> str:
    return value.replace("T", " ")[:16] if value else "never"


def print_table(rows: list[dict], *, show_plugin: bool) -> None:
    if not rows:
        print("No keys.")
        return
    head = f"{'#':>2}  {'last used':<16}  {'created':<16}  "
    print(head + (f"{'plugin':<12}  id" if show_plugin else "id"))
    for n, row in enumerate(rows, 1):
        mark = "  <- this machine" if row["this_machine"] else ""
        line = f"{n:>2}  {_fmt(row['last_used_at']):<16}  {_fmt(row['created_at']):<16}  "
        if show_plugin:
            line += f"{row['plugin']:<12}  "
        print(line + row["id"] + mark)


def cmd_list(args, key: str, service_url: str) -> int:
    plugins = known_plugin_keys(key, service_url) if args.all else [args.plugin or PLUGIN_KEY]
    rows: list[dict] = []
    unsupported: list[str] = []
    for plugin in plugins:
        try:
            rows.extend(list_keys(plugin, key=key, service_url=service_url))
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 405):
                unsupported.append(plugin)
                continue
            raise
    rows = sort_by_last_use(rows)
    if not args.all and unsupported:
        return _fail(
            args,
            5,
            "no_key_routes",
            f"The server has no key routes for {plugins[0]!r}: it predates per-machine "
            "plugin keys (cognee#6113) or does not register this plugin.",
        )
    if args.json:
        print(json.dumps({"ok": True, "keys": rows, "unsupported": unsupported}))
        return 0
    print_table(rows, show_plugin=args.all)
    if rows:
        hint = " --plugin <plugin>" if args.all else ""
        print(f"\nRevoke one: plugin-keys.py revoke <id>{hint}")
    return 0


def cmd_revoke(args, key: str, service_url: str) -> int:
    plugin = args.plugin or PLUGIN_KEY
    mine = plugin == PLUGIN_KEY and args.key_id == this_machine_key_id(service_url)
    if mine and not args.force:
        return _fail(
            args,
            4,
            "this_machine",
            "That is the key this machine is using. Revoke it with --force if you mean "
            "to; the next session start provisions a new one.",
        )
    try:
        _request(
            f"/api/v1/integrations/plugins/{urllib.parse.quote(plugin, safe='')}/keys/"
            f"{urllib.parse.quote(args.key_id, safe='')}",
            method="DELETE",
            key=key,
            service_url=service_url,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _fail(
                args,
                3,
                "not_found",
                f"No key {args.key_id} on the {plugin!r} agent (already revoked, or another "
                "plugin's).",
            )
        raise
    if mine:
        clear_cached_agent_key()
    if args.json:
        print(
            json.dumps({"ok": True, "revoked": args.key_id, "plugin": plugin, "this_machine": mine})
        )
        return 0
    print(f"Revoked key {args.key_id} of the {plugin!r} agent.")
    if mine:
        print("That was this machine's key: the next session start provisions a new one.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    lst = sub.add_parser("list", help="List the agent's keys, most recently used first")
    lst.add_argument("--plugin", default="", help=f"Plugin key (default: {PLUGIN_KEY})")
    lst.add_argument("--all", action="store_true", help="Every plugin agent of this user")
    lst.add_argument("--json", action="store_true")
    rev = sub.add_parser("revoke", help="Revoke one key by id")
    rev.add_argument("key_id")
    rev.add_argument("--plugin", default="", help=f"Plugin key (default: {PLUGIN_KEY})")
    rev.add_argument("--force", action="store_true", help="Allow revoking this machine's own key")
    rev.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    service_url = _normalize_service_url(_local_api_url())
    key = principal_key(service_url)
    if not key:
        return _fail(
            args,
            2,
            "no_principal_key",
            "No principal API key resolved (COGNEE_PRINCIPAL_API_KEY, COGNEE_API_KEY, "
            "~/.cognee-plugin/api_key.json). Start a session first, or set COGNEE_API_KEY.",
        )
    try:
        if args.command == "list":
            return cmd_list(args, key, service_url)
        return cmd_revoke(args, key, service_url)
    except urllib.error.HTTPError as exc:
        return _fail(args, 1, "http", f"{exc.code} from {service_url}: {str(exc)[:200]}")
    except Exception as exc:
        return _fail(args, 1, "unreachable", f"Request to {service_url} failed: {str(exc)[:200]}")


if __name__ == "__main__":
    sys.exit(main())
