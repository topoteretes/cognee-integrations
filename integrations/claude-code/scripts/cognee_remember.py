#!/usr/bin/env python3
"""Store text in Cognee's permanent knowledge graph.

Usage:
  cognee_remember.py <content> [--node-set <node_set>] [--dataset <dataset>]
  cognee_remember.py --file <path> [--node-set <node_set>] [--dataset <dataset>]

--node-set: node set for categorization (default: user_context)
            user_context | project_docs | agent_actions
--dataset:  dataset name (default: this launch's active dataset); under
            shared memory it resolves to the user's canonical copy, as a
            dataset switch does. "auto" is reserved for auto recall and refused.
--file:     upload a file under its REAL filename instead of inline text.
            The extension is the server's routing signal: code files
            (.py/.ts/.go/...) ride the zero-LLM code-graph route on
            cognee >= 1.5.x; anything else flows through its normal loader.
            With --file, positional <content> is not required.

Configuration:
  Resolves auth from the plugin option (hooks) or the local server. In Cloud
  mode a shell has no key (Claude Code passes the Cloud key option to hooks
  only), so a save run from a shell exits 2 before any request.
  Dataset comes from this launch's record (follows a dataset switch), then
  COGNEE_PLUGIN_DATASET, then agent_sessions.
  Server only: an unreachable server prints UNREACHABLE and exits 1, and the
  content is not saved (no CLI fallback).

The hooks module runs this file directly (one fixed script, no shell);
cognee-remember.sh is a thin wrapper around it for the skills.
"""

from __future__ import annotations

import os
import sys

from _env_file import cloud_key, load_env_file

# One-time config from ~/.cognee/.env (shell exports still win).
load_env_file()

UNREACHABLE = "UNREACHABLE"


def _runtime() -> dict:
    """The server URL, key and dataset this launch writes with."""
    from cognee_search import _cached_api_key, _configured_dataset, _require_shell_auth

    service_url = (
        os.environ.get("COGNEE_BASE_URL")
        or os.environ.get("COGNEE_LOCAL_API_URL")
        or "http://localhost:8011"
    ).strip()
    api_key = cloud_key(service_url) or _cached_api_key(service_url)
    dataset = _configured_dataset()
    dataset_id = ""
    # The launch record wins (dataset chosen with switch-dataset.py, its
    # canonical UUID under shared memory, the plugin-agent key the hooks use).
    # The opt-out keeps _plugin_common from re-execing this write into the
    # plugin venv, which would run it twice.
    previous = os.environ.get("COGNEE_PLUGIN_IN_VENV")
    os.environ["COGNEE_PLUGIN_IN_VENV"] = "1"
    try:
        import _plugin_common

        rt = _plugin_common.shell_runtime_overrides(service_url)
        dataset = rt["dataset"] or dataset
        dataset_id = rt["dataset_id"]
        api_key = rt["api_key"] or api_key
    except Exception:
        pass
    finally:
        if previous is None:
            os.environ.pop("COGNEE_PLUGIN_IN_VENV", None)
        else:
            os.environ["COGNEE_PLUGIN_IN_VENV"] = previous
    # Before any request: a remote server with no key here can only 401.
    _require_shell_auth(service_url, api_key, "cognee-remember")
    return {
        "service_url": service_url,
        "api_key": api_key,
        "dataset": dataset or _configured_dataset() or "agent_sessions",
        "dataset_id": dataset_id,
    }


def _shared_dataset_id(dataset: str) -> str:
    """The canonical UUID an explicit --dataset name resolves to ("" if none).

    Resolved the way a dataset switch does: under shared memory, to the
    canonical copy the user owns (created if missing). Sent by name instead,
    the server would find or create an agent-owned copy of the same name that
    nobody else sees. Separated memory keeps the name. A UUID already addresses
    one dataset (_remember_http sends it as datasetId).
    """
    try:
        from _dataset_access import dataset_id as as_dataset_id
        from _plugin_common import resolve_shared_dataset

        if as_dataset_id(dataset):
            return ""
        shared = resolve_shared_dataset(dataset)
        return str(shared["dataset_id"]) if shared.get("mode") == "shared" else ""
    except Exception:
        return ""


def main(argv: list[str]) -> int:
    rt = _runtime()
    dataset = rt["dataset"]
    dataset_id = rt["dataset_id"]

    # Content is the first positional (unless --file leads); flags follow.
    content = ""
    node_set = "user_context"
    file_path = ""
    dataset_explicit = False
    args = list(argv)
    if args and args[0] != "--file":
        content = args.pop(0)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--node-set":
            i += 1
            node_set = (args[i] if i < len(args) else "") or "user_context"
        elif arg in ("--dataset", "-d"):
            i += 1
            dataset = (args[i] if i < len(args) else "") or dataset
            # "auto" is reserved for auto recall (SDK-1393): no save lands in a
            # dataset that could never be pinned afterwards.
            if " ".join(dataset.split()).lower() == "auto":
                print(
                    "Error: 'auto' is reserved for auto recall, not a dataset name", file=sys.stderr
                )
                return 2
            # An explicit name wins over the launch's dataset; its canonical
            # UUID is resolved below, after every flag is read.
            dataset_id = ""
            dataset_explicit = True
        elif arg == "--file":
            i += 1
            file_path = args[i] if i < len(args) else ""
        i += 1

    if file_path and not os.path.isfile(file_path):
        print(f"Error: --file path not found: {file_path}", file=sys.stderr)
        return 1
    if not content and not file_path:
        print("Error: no content provided", file=sys.stderr)
        return 1

    if dataset_explicit:
        dataset_id = _shared_dataset_id(dataset)

    # Server only: POST to /api/v1/remember through _remember_http, which
    # prints the result. Any result other
    # than UNREACHABLE (ok or error) is authoritative and is not retried;
    # UNREACHABLE means nothing was saved.
    import _remember_http

    result = UNREACHABLE
    try:
        result = _remember_http.do_remember(
            rt["service_url"],
            rt["api_key"],
            content,
            dataset,
            node_set,
            file_path=file_path or None,
            dataset_id=dataset_id,
            timeout=_remember_http._remember_timeout(),
        )
    except Exception as exc:
        print(f"[cognee-remember] remember client failed: {exc}", file=sys.stderr)
    if result != UNREACHABLE:
        _remember_http.report(result)
        return 0
    print(
        "[cognee-remember] server unreachable — content not saved; retry once the server is back",
        file=sys.stderr,
    )
    print(UNREACHABLE)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
