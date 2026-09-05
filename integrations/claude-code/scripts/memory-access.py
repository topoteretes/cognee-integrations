#!/usr/bin/env python3
"""Manage plugin memory access using explicit IDs and authenticated API calls."""

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _plugin_common as pc
from _dataset_access import dataset_id


def owner_credentials():
    url = pc._local_api_url()
    cached_agent = pc.load_cached_agent_key(url)
    key = os.environ.get("COGNEE_API_KEY", "").strip()
    if not key or key == cached_agent:
        key = os.environ.get("COGNEE_PRINCIPAL_API_KEY", "").strip() or pc.load_cached_api_key(url)
    if not key or key == cached_agent:
        raise RuntimeError("An explicit principal credential is required to manage access")
    me = pc._json_http_request("/api/v1/users/me", method="GET", api_key=key)
    if me.get("parent_user_id") or me.get("parentUserId"):
        raise RuntimeError("Access management requires the user principal, not an agent credential")
    return key, me


def change_permission(principal_id, dataset_ids, permission, *, revoke=False):
    ident = dataset_id(principal_id)
    ids = [dataset_id(value) for value in dataset_ids]
    if not ident or not ids or not all(ids) or permission not in ("read", "write"):
        raise ValueError("Use principal and dataset UUIDs, with read or write permission")
    key, _ = owner_credentials()
    return pc._json_http_request(
        f"/api/v1/permissions/datasets/{ident}?permission_name={permission}",
        ids,
        method="DELETE" if revoke else "POST",
        api_key=key,
    )


def set_read_scope(host_key, dataset_ids):
    if not pc._read_map_record(host_key).get("session_id"):
        raise RuntimeError("No active launch record; pass the host session ID")
    ids = [dataset_id(value) for value in dataset_ids]
    if not all(ids):
        raise ValueError("Read datasets must be UUIDs")
    readable = pc._json_http_request("/api/v1/datasets/", method="GET")
    allowed = {str(row.get("id")) for row in readable}
    if not set(ids).issubset(allowed):
        raise RuntimeError("The plugin cannot read every selected dataset; grant access first")
    scope = {
        "base_url": pc._normalize_service_url(pc._local_api_url()),
        "credential_fingerprint": pc._principal_fingerprint(pc._api_key()),
        "dataset_ids": list(dict.fromkeys(ids)),
    }
    path = pc.graph_read_scope_path(host_key)
    pc._write_json_file(path, scope)
    if pc._load_json_file(path) != scope:
        raise RuntimeError("Read scope was not persisted")
    return {"read_dataset_ids": scope["dataset_ids"], "session_key": host_key}


def connect_existing_identity(agent_key):
    """Import a supplied key; never mint or rotate credentials implicitly."""
    owner_key, owner = owner_credentials()
    if not agent_key or agent_key == owner_key:
        raise ValueError("Supply the plugin agent's key, not the principal key")
    agent = pc._json_http_request("/api/v1/users/me", method="GET", api_key=agent_key)
    parent = agent.get("parent_user_id") or agent.get("parentUserId")
    if str(parent or "") != str(owner.get("id")):
        raise RuntimeError("The supplied agent does not belong to the authenticated user")
    status = pc._json_http_request("/api/v1/integrations/status", method="GET", api_key=owner_key)
    plugins = status.get("plugins", [])
    rows = [row for row in plugins if row.get("key") == pc.PLUGIN_KEY]
    if not rows or str(rows[0].get("agentId") or rows[0].get("agent_id")) != str(agent.get("id")):
        raise RuntimeError("The supplied key is not this plugin's registered identity")
    with pc.plugin_identity_lock():
        pc.save_cached_agent_key(
            pc._local_api_url(), agent_key, str(agent["id"]), principal_key=owner_key
        )
    return {"connected": True, "agent_id": agent["id"], "next_step": "Start a fresh host session"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    for verb in ("grant", "revoke"):
        child = sub.add_parser(verb)
        child.add_argument("--principal-id", required=True)
        child.add_argument("--dataset-id", action="append", required=True)
        child.add_argument("--permission", choices=("read", "write"), required=True)
    child = sub.add_parser("read")
    child.add_argument("--session-key", required=True)
    child.add_argument("--dataset-id", action="append", default=[])
    child = sub.add_parser("write")
    child.add_argument("dataset_id")
    child.add_argument("--session-key", required=True)
    child = sub.add_parser("connect")
    child.add_argument("--key-env", required=True, help="Name of env var holding the agent key")
    args = parser.parse_args(argv)
    if args.command in ("grant", "revoke"):
        result = change_permission(
            args.principal_id, args.dataset_id, args.permission, revoke=args.command == "revoke"
        )
    elif args.command == "read":
        result = set_read_scope(args.session_key, args.dataset_id)
    elif args.command == "connect":
        result = connect_existing_identity(os.environ.get(args.key_env, ""))
    elif args.command == "write":
        if not dataset_id(args.dataset_id):
            raise ValueError("Select a dataset UUID")
        path = Path(__file__).with_name("switch-dataset.py")
        spec = importlib.util.spec_from_file_location("switch_dataset", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        host, record = module._resolve_launch(args.session_key)
        result = module._switch(host, record, args.dataset_id, force=False)
    else:
        me = pc._json_http_request("/api/v1/users/me", method="GET")
        result = {
            "plugin": pc.PLUGIN_KEY,
            "identity": me.get("id"),
            "readable_datasets": pc._json_http_request("/api/v1/datasets/", method="GET"),
            "datasets": pc.list_writable_datasets(str(me.get("id") or "")),
        }
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(1) from None
