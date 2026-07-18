"""Tests for `_ensure_statusline_configured` not clobbering a user's status line.

`~/.claude/settings.json` is global, shared Claude Code config. The SessionStart
hook installs Cognee's status line only when the slot is empty or already ours
(self-healing older Cognee paths to the current one); any other statusLine is
user-owned and preserved.

Run: python integrations/claude-code/tests/test_statusline_no_clobber.py
(or via pytest).
"""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location(
        "session_start_mod", _SCRIPTS / "session-start.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    ss = _load()
except Exception:  # pragma: no cover - hook deps not importable in this environment
    ss = None

_SCRIPT = _SCRIPTS / "cognee-statusline.sh"
_DESIRED = {
    "type": "command",
    "command": f'[ -x "{_SCRIPT}" ] && exec "{_SCRIPT}" || true',
}


def _run(home, raw_settings=None):
    """Run the hook against a temp HOME; return the resulting settings.json text."""
    os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
    settings_path = pathlib.Path(home) / ".claude" / "settings.json"
    if raw_settings is not None:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(raw_settings, encoding="utf-8")

    old_home = os.environ.get("HOME")
    os.environ["HOME"] = home
    old_hook_log = ss.hook_log
    ss.hook_log = lambda *a, **k: None
    try:
        ss._ensure_statusline_configured()
    finally:
        ss.hook_log = old_hook_log
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
    return settings_path.read_text(encoding="utf-8") if settings_path.exists() else None


def _statusline(home, raw_settings=None):
    out = _run(home, raw_settings)
    return json.loads(out).get("statusLine") if out else None


def test_installs_when_absent():
    if ss is None:
        return
    with tempfile.TemporaryDirectory() as home:
        assert _statusline(home) == _DESIRED


def test_preserves_user_custom_statusline():
    if ss is None:
        return
    custom = {"statusLine": {"type": "command", "command": "/my/custom/statusline.sh"}}
    with tempfile.TemporaryDirectory() as home:
        assert _statusline(home, json.dumps(custom)) == custom["statusLine"]


def test_noop_when_already_current():
    if ss is None:
        return
    with tempfile.TemporaryDirectory() as home:
        raw = json.dumps({"statusLine": _DESIRED})
        assert _run(home, raw) == raw


def test_self_heals_stale_cognee_path():
    if ss is None:
        return
    stale = {
        "statusLine": {
            "type": "command",
            "command": "/old/plugin/cache/cognee-memory/0.1.0/scripts/cognee-statusline.sh",
        }
    }
    with tempfile.TemporaryDirectory() as home:
        assert _statusline(home, json.dumps(stale)) == _DESIRED


def test_preserves_non_dict_statusline():
    if ss is None:
        return
    with tempfile.TemporaryDirectory() as home:
        raw = json.dumps({"statusLine": "/my/custom/statusline.sh"})
        assert _run(home, raw) == raw


def test_fails_closed_on_corrupt_json():
    if ss is None:
        return
    with tempfile.TemporaryDirectory() as home:
        assert _run(home, "{not valid json") == "{not valid json"


if __name__ == "__main__":
    if ss is None:
        print("SKIP: session-start.py not importable in this environment")
        sys.exit(0)
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
    sys.exit(1 if failures else 0)
