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


@pytest.fixture(autouse=True)
def _only_cursor(suite):
    if suite.name != "cursor":
        pytest.skip(f"{suite.name}: the status-bar picker is Cursor's")


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
    auto = {
        "current": "team-id",
        "recall": "auto",
        "message": "Recall is auto: the server picks the best-matching dataset for each recall.",
        "datasets": [
            {"name": "team", "id": "team-id", "current": True, "writable": True},
            {"name": "demo", "id": "demo-id", "current": False, "writable": True},
            {"name": "pin", "id": "pin-id", "current": False, "writable": True},
        ],
    }
    pinned = {
        "current": "a",
        "recall": "pinned",
        "reason": "configured_pin",
        "message": "Recall is pinned to 'a', where saves go, because "
        "COGNEE_PLUGIN_RECALL_MODE=pinned.",
        "datasets": [{"name": "a", "id": "1", "current": True, "writable": True}],
    }
    auto_items, pinned_items = (_by_label(items) for items in _run([auto, pinned], [])["items"])
    assert auto_items["auto (best-matching dataset)"]["target"] == "auto"
    assert auto_items["auto (best-matching dataset)"]["detail"] == auto["message"]
    assert auto_items["team"]["target"] == "pin"
    assert auto_items["demo"]["target"] == "demo"
    assert auto_items["pin"]["target"] == "pin-id"
    # Pinned: the script's message moves to the active row.
    assert pinned_items["a"]["target"] == "pin" and pinned_items["a"]["current"] is True
    assert pinned_items["a"]["detail"] == pinned["message"]
    assert "COGNEE_PLUGIN_DATASET" not in pinned_items["a"]["description"]
    assert "detail" not in pinned_items["auto (best-matching dataset)"]


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
            [{"switched": True, "dataset": "b"}, "b"],
        ],
    )
    assert out["messages"] == [said, "this chat now uses b."]


def test_only_the_row_marked_current_is_current_among_same_named_datasets():
    """Under shared memory the listing can carry two rows with the active name
    (the person's own and another principal's copy). Only the one the script
    marks is current; picking the other switches to it by id, not ``pin``."""
    listing = {
        "current": "agent_sessions",
        "recall": "pinned",
        "datasets": [
            {"name": "agent_sessions", "id": "mine", "current": True, "writable": True},
            {"name": "agent_sessions", "id": "theirs", "current": False, "writable": True},
            {"name": "demo", "id": "demo-id", "current": False, "writable": True},
        ],
    }
    items = _run([listing], [])["items"][0]
    rows = [item for item in items if item["label"] == "agent_sessions"]
    assert len(rows) == 2
    mine, theirs = rows
    assert mine["current"] is True and mine["target"] == "pin"
    assert mine["description"].startswith("current")
    assert theirs["current"] is False and theirs["target"] == "theirs"
    assert theirs["description"] == "" and "detail" not in theirs
    assert _by_label(items)["demo"]["target"] == "demo"


def test_an_older_listing_without_current_marks_the_active_row_by_name():
    listing = {
        "current": "a",
        "recall": "pinned",
        "datasets": [
            {"name": "a", "id": "1", "writable": True},
            {"name": "b", "id": "2", "writable": True},
        ],
    }
    items = _by_label(_run([listing], [])["items"][0])
    assert items["a"]["current"] is True and items["a"]["target"] == "pin"
    assert items["b"]["current"] is False and items["b"]["target"] == "b"


def test_the_active_dataset_missing_from_the_listing_heads_it():
    listing = {
        "current": "read-only",
        "recall": "pinned",
        "datasets": [{"name": "b", "id": "2", "current": False, "writable": True}],
    }
    items = _run([listing], [])["items"][0]
    assert items[1]["label"] == "read-only" and items[1]["target"] == "pin"
    assert items[2]["label"] == "b" and items[2]["current"] is False
