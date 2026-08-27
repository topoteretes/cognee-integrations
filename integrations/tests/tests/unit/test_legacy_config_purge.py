"""SessionStart removes a leftover ``config.json``.

The file was an older config layer that SessionStart read but the per-turn hooks
never did, so a stale ``base_url`` in it pointed the two halves of the plugin at
different servers (SDK-466). Nothing reads it any more; leaving it on disk would
only mislead whoever opens it next. Both historical locations are covered: the
shared ``~/.cognee-plugin`` root (codex) and the suite's own state dir
(claude-code).
"""

from __future__ import annotations

import json

from utils.suites import plugin_root, state_dir


def test_session_start_removes_legacy_config_json(suite, hook_module, temp_home):
    ss = hook_module(suite, "session-start.py")
    planted = [
        plugin_root(temp_home) / "config.json",
        state_dir(suite, temp_home) / "config.json",
    ]
    for path in planted:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"base_url": "https://stale.example"}), encoding="utf-8")

    ss._purge_legacy_resolved_files()

    # The suite's own location is gone; the other suite's is not this suite's to touch.
    own = (plugin_root(temp_home) if suite.name == "codex" else state_dir(suite, temp_home)) / "config.json"
    assert not own.exists()
    # Idempotent: a second run with nothing to remove must not raise.
    ss._purge_legacy_resolved_files()
