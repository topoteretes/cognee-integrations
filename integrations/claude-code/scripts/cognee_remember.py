#!/usr/bin/env python3
"""Store text in Cognee's permanent knowledge graph.

Usage:
  cognee_remember.py <content> [--node-set <node_set>] [--dataset <dataset>]
  cognee_remember.py --file <path> [--node-set <node_set>] [--dataset <dataset>]

--node-set: node set for categorization (default: user_context)
            user_context | project_docs | agent_actions
--dataset:  dataset name (default: this launch's active dataset)
--file:     upload a file under its REAL filename instead of inline text.
            The extension is the server's routing signal: code files
            (.py/.ts/.go/...) ride the zero-LLM code-graph route on
            cognee >= 1.5.x; anything else flows through its normal loader.
            With --file, positional <content> is not required.

Configuration:
  Resolves auth from env/api_key.json.
  Dataset comes from this launch's record (follows a dataset switch), then
  COGNEE_PLUGIN_DATASET, then agent_sessions.
  Falls back to cognee-cli only if the server is unreachable.

The hooks module runs this file directly (one fixed script, no shell);
cognee-remember.sh is a thin wrapper around it for the skills.
"""

from __future__ import annotations

import os
import subprocess
import sys

from _env_file import load_env_file

# One-time config from ~/.cognee/.env (shell exports still win).
load_env_file()

UNREACHABLE = "UNREACHABLE"


def _runtime() -> dict:
    """The server URL, key and dataset this launch writes with."""
    from cognee_search import _cached_api_key

    service_url = (
        os.environ.get("COGNEE_BASE_URL")
        or os.environ.get("COGNEE_LOCAL_API_URL")
        or "http://localhost:8011"
    ).strip()
    api_key = (os.environ.get("COGNEE_API_KEY") or "").strip() or _cached_api_key(service_url)
    dataset = (os.environ.get("COGNEE_PLUGIN_DATASET") or "").strip()
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
    return {
        "service_url": service_url,
        "api_key": api_key,
        "dataset": dataset or os.environ.get("COGNEE_PLUGIN_DATASET") or "agent_sessions",
        "dataset_id": dataset_id,
    }


def main(argv: list[str]) -> int:
    rt = _runtime()
    dataset = rt["dataset"]
    dataset_id = rt["dataset_id"]

    # Content is the first positional (unless --file leads); flags follow.
    content = ""
    node_set = "user_context"
    file_path = ""
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
            # The canonical UUID belongs to the launch's active dataset only.
            dataset_id = ""
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

    # Server-first: POST to /api/v1/remember through _remember_http, which
    # prints the result (and refreshes the credits marker). UNREACHABLE → fall
    # back to cognee-cli and warn; any other result (ok or error) is
    # authoritative and is not retried.
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
    if file_path:
        # No CLI fallback for file uploads: the CLI path would re-read and
        # rename the content, losing the extension-based code routing this
        # mode exists for.
        print(
            "[cognee-remember] server unreachable — file upload NOT stored; "
            "retry once the server is back",
            file=sys.stderr,
        )
        print(UNREACHABLE)
        return 1
    print(
        "[cognee-remember] falling back to cognee-cli (degraded — server unreachable; "
        "verify the store succeeded once the server is back)",
        file=sys.stderr,
    )
    # The server path sanitizes inside _remember_http; the CLI gets the same
    # text. A failure stops here instead of storing the raw text.
    try:
        from _capture_policy import sanitize

        content = sanitize(content)
    except Exception as exc:
        print(f"[cognee] could not sanitize the text: {exc}", file=sys.stderr)
        return 1
    try:
        ran = subprocess.run(
            ["cognee-cli", "remember", content, "-d", dataset, "--node-set", node_set],
            capture_output=True,
            text=True,
        )
        sys.stdout.write(ran.stdout)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
