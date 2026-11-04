"""The Cursor status-bar picker (``cursor/statusbar/extension.js``).

The extension is plain JavaScript; its pure helpers are exported, so node loads
the file with a stand-in ``vscode`` module and the test checks what a pick
sends to ``switch-dataset.py`` and what the notification says:

  * the active dataset, picked, is ``pin`` (recall restricted to it, no new
    session), whether the launch record holds it by name or, for a dataset
    another principal owns, by id; another dataset named "pin" goes by its id;
  * the notification and the active row's detail are the script's ``message``,
    with a generic line only when an older script sends none.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

EXTENSION = Path(__file__).resolve().parents[3] / "cursor" / "statusbar" / "extension.js"

_HARNESS = r"""
const Module = require("module");
const original = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "vscode") return {};
  return original.apply(this, arguments);
};
const ext = require(process.argv[1]);
const input = JSON.parse(process.argv[2]);
const out = {
  items: input.listings.map((data) => ext.pickerItems(data)),
  messages: input.results.map(([result, name]) => ext.switchMessage(result, name)),
};
process.stdout.write(JSON.stringify(out));
"""


def _run(listings: list[dict], results: list[list]) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    proc = subprocess.run(
        [
            node,
            "-e",
            _HARNESS,
            str(EXTENSION),
            json.dumps({"listings": listings, "results": results}),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _by_label(items: list[dict]) -> dict:
    return {item["label"]: item for item in items}


def test_the_active_dataset_is_picked_as_pin_and_others_by_name_or_id():
    listing = {
        "current": "team-id",
        "recall": "auto",
        "message": "Recall is auto: the server picks the best-matching dataset for each recall.",
        "datasets": [
            {"name": "team", "id": "team-id", "current": True, "writable": True},
            {"name": "demo", "id": "demo-id", "current": False, "writable": True},
            {"name": "pin", "id": "pin-id", "current": False, "writable": True},
        ],
    }
    items = _by_label(_run([listing], [])["items"][0])
    assert items["auto (best-matching dataset)"]["target"] == "auto"
    assert items["auto (best-matching dataset)"]["detail"] == listing["message"]
    assert items["team"]["target"] == "pin"
    assert items["demo"]["target"] == "demo"
    assert items["pin"]["target"] == "pin-id"


def test_a_pinned_listing_puts_the_script_message_on_the_active_row():
    message = "Recall is pinned to 'a', where saves go, because COGNEE_PLUGIN_RECALL_MODE=pinned."
    listing = {
        "current": "a",
        "recall": "pinned",
        "reason": "configured_pin",
        "message": message,
        "datasets": [{"name": "a", "id": "1", "current": True, "writable": True}],
    }
    items = _by_label(_run([listing], [])["items"][0])
    assert items["a"]["target"] == "pin" and items["a"]["current"] is True
    assert items["a"]["detail"] == message
    assert "COGNEE_PLUGIN_DATASET" not in items["a"]["description"]
    assert "detail" not in items["auto (best-matching dataset)"]


def test_the_notification_is_the_script_message_with_a_generic_fallback():
    said = "Recall is now pinned to 'a', the active dataset; saves go there too."
    out = _run(
        [],
        [
            [
                {
                    "switched": False,
                    "reason": "recall_restricted",
                    "recall": "pinned",
                    "message": said,
                },
                "a",
            ],
            [
                {"switched": False, "reason": "recall_auto", "recall": "auto", "dataset": "a"},
                "auto",
            ],
            [{"switched": True, "dataset": "b"}, "b"],
        ],
    )
    assert out["messages"] == [
        said,
        "recall is auto (saves go to a).",
        "this chat now uses b.",
    ]
