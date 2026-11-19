"""The Claude Code plugin takes the server URL from its option only.

``COGNEE_BASE_URL`` in the shell or in ``~/.cognee/.env`` used to be the URL
when the option was empty. An edit of the file then moved a running session to
another server while the cloud worker, which had inherited the old URL from the
hook that started it, kept serving the old one and marking it ready, which hid
the new server's rejected key. The mode now follows the two connection options:

* both empty: local mode;
* the server URL option set: that server;
* only the API key option set: cloud mode with no server to connect to, the
  state of ``COGNEE_BACKEND=cloud`` with no URL (no local server is started,
  the key is never sent to the local default, the status line shows
  ``missing_cognee_base_url``).

A ``COGNEE_BASE_URL`` found in the shell or the file is never used and is
named (name only) so session start can say so. Each case runs in a fresh
interpreter, as each hook does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from utils.suites import CLAUDE

_SCRIPTS = CLAUDE.scripts_dir
_REMOTE = "https://tenant.cognee.test"
_FILE_URL = "https://from-the-file.cognee.test"
_WINDOWS_SYSTEM_VARS = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")

_RESOLVED = """
import json, os, _env_file
_env_file.load_env_file()
import config, _plugin_common
cfg = config.load_config()
print(json.dumps({
    "url": os.environ.get("COGNEE_BASE_URL"),
    "switch": os.environ.get("COGNEE_CLAUDE_BACKEND"),
    "forced": _env_file.forced_backend(),
    "cloud": config.is_cloud_mode(cfg),
    "base_url": cfg.get("base_url"),
    "key_to_default": _plugin_common.user_cloud_key("http://localhost:8011"),
    "ignored": _env_file.ignored_key_names(),
}))
"""


def _resolve(home: Path, *, hook: bool = True, env_file: str = "", **env: str) -> dict:
    base = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home)}
    base.update({name: os.environ[name] for name in _WINDOWS_SYSTEM_VARS if name in os.environ})
    base.update({"COGNEE_PLUGIN_IN_VENV": "1", "COGNEE_IDLE_DISABLED": "1"})
    if hook:
        base["CLAUDE_PLUGIN_ROOT"] = str(_SCRIPTS.parent)
    if env_file:
        path = home / ".cognee" / ".env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(env_file, encoding="utf-8")
    base.update(env)
    ran = subprocess.run(
        [sys.executable, "-c", _RESOLVED],
        cwd=str(_SCRIPTS),
        env=base,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ran.returncode == 0, ran.stderr
    return json.loads(ran.stdout.strip().splitlines()[-1])


def test_both_options_empty_is_local_whatever_the_shell_and_file_say(tmp_path):
    seen = _resolve(
        tmp_path,
        env_file=f'COGNEE_BASE_URL="{_FILE_URL}"\n',
        COGNEE_BASE_URL="https://exported.cognee.test",
    )

    assert seen["url"] is None
    assert seen["forced"] == "" and seen["cloud"] is False
    assert "COGNEE_BASE_URL" in seen["ignored"]


def test_the_url_option_is_the_server_and_the_file_cannot_move_it(tmp_path):
    seen = _resolve(
        tmp_path,
        env_file=f'COGNEE_BASE_URL="{_FILE_URL}"\n',
        CLAUDE_PLUGIN_OPTION_COGNEE_BASE_URL=_REMOTE,
        CLAUDE_PLUGIN_OPTION_COGNEE_API_KEY="ck_option",
    )

    assert seen["url"] == _REMOTE and seen["base_url"] == _REMOTE
    assert seen["cloud"] is True
    assert "COGNEE_BASE_URL" in seen["ignored"]


def test_a_file_url_equal_to_the_option_is_not_reported(tmp_path):
    seen = _resolve(
        tmp_path,
        env_file=f'COGNEE_BASE_URL="{_REMOTE}"\n',
        CLAUDE_PLUGIN_OPTION_COGNEE_BASE_URL=_REMOTE,
    )

    assert "COGNEE_BASE_URL" not in seen["ignored"]


def test_a_key_without_a_url_is_cloud_with_nothing_to_connect_to(tmp_path):
    seen = _resolve(
        tmp_path,
        env_file=f'COGNEE_BASE_URL="{_FILE_URL}"\n',
        CLAUDE_PLUGIN_OPTION_COGNEE_API_KEY="ck_option",
    )

    assert seen["url"] is None and not seen["base_url"]
    assert seen["forced"] == "cloud" and seen["cloud"] is True
    # Never sent to whatever listens on the local default.
    assert seen["key_to_default"] == ""
    # Decided in the process, not written into the environment children inherit.
    assert seen["switch"] is None


def test_processes_without_options_follow_what_a_hook_recorded(tmp_path):
    _resolve(tmp_path, CLAUDE_PLUGIN_OPTION_COGNEE_API_KEY="ck_option")
    statusline = _resolve(tmp_path, hook=False, COGNEE_BASE_URL=_FILE_URL)
    assert statusline["url"] is None and statusline["forced"] == "cloud"

    _resolve(tmp_path, CLAUDE_PLUGIN_OPTION_COGNEE_BASE_URL=_REMOTE)
    assert _resolve(tmp_path, hook=False)["url"] == _REMOTE

    _resolve(tmp_path)  # both options emptied
    emptied = _resolve(tmp_path, hook=False, COGNEE_BASE_URL=_FILE_URL)
    assert emptied["url"] is None and emptied["forced"] == ""


def test_an_exported_backend_switch_still_wins(tmp_path):
    seen = _resolve(
        tmp_path,
        CLAUDE_PLUGIN_OPTION_COGNEE_API_KEY="ck_option",
        COGNEE_BACKEND="local",
    )

    assert seen["forced"] == "local" and seen["cloud"] is False
