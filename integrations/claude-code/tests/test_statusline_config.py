"""Tests for Claude Code statusLine setup behavior.

Run: python integrations/claude-code/tests/test_statusline_config.py
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
    mod.hook_log = lambda *_args, **_kwargs: None
    return mod


try:
    ss = _load()
except Exception:  # pragma: no cover - hook deps not importable in this environment
    ss = None


def _with_temp_home(fn):
    old_home = os.environ.get("HOME")
    old_statusline = os.environ.get("COGNEE_STATUSLINE")
    old_plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.environ["HOME"] = tmp
            os.environ.pop("COGNEE_STATUSLINE", None)
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
            return fn(pathlib.Path(tmp))
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
            if old_statusline is None:
                os.environ.pop("COGNEE_STATUSLINE", None)
            else:
                os.environ["COGNEE_STATUSLINE"] = old_statusline
            if old_plugin_root is None:
                os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
            else:
                os.environ["CLAUDE_PLUGIN_ROOT"] = old_plugin_root


def _settings_path(home):
    return home / ".claude" / "settings.json"


def test_statusline_written_when_absent():
    if ss is None:
        return

    def run(home):
        ss._ensure_statusline_configured()
        settings = json.loads(_settings_path(home).read_text(encoding="utf-8"))
        assert "cognee-statusline.sh" in settings["statusLine"]["command"]

    _with_temp_home(run)


def test_existing_user_statusline_is_preserved():
    if ss is None:
        return

    def run(home):
        path = _settings_path(home)
        path.parent.mkdir(parents=True)
        original = {"type": "command", "command": "printf custom-status"}
        path.write_text(json.dumps({"statusLine": original}) + "\n", encoding="utf-8")

        ss._ensure_statusline_configured()

        settings = json.loads(path.read_text(encoding="utf-8"))
        assert settings["statusLine"] == original

    _with_temp_home(run)


def test_existing_non_dict_statusline_is_preserved():
    if ss is None:
        return

    def run(home):
        path = _settings_path(home)
        path.parent.mkdir(parents=True)
        original = "/my/custom/statusline.sh"
        path.write_text(json.dumps({"statusLine": original}), encoding="utf-8")

        ss._ensure_statusline_configured()

        settings = json.loads(path.read_text(encoding="utf-8"))
        assert settings["statusLine"] == original

    _with_temp_home(run)


def test_cognee_marker_outside_command_does_not_claim_statusline():
    if ss is None:
        return

    def run(home):
        path = _settings_path(home)
        path.parent.mkdir(parents=True)
        original = {
            "type": "command",
            "command": "printf custom-status",
            "description": "not the cognee-statusline command",
        }
        path.write_text(json.dumps({"statusLine": original}), encoding="utf-8")

        ss._ensure_statusline_configured()

        settings = json.loads(path.read_text(encoding="utf-8"))
        assert settings["statusLine"] == original

    _with_temp_home(run)


def test_similarly_named_custom_statusline_is_preserved():
    if ss is None:
        return

    def run(home):
        path = _settings_path(home)
        path.parent.mkdir(parents=True)
        original = {
            "type": "command",
            "command": "/usr/local/bin/cognee-statusline-custom",
        }
        path.write_text(json.dumps({"statusLine": original}), encoding="utf-8")

        ss._ensure_statusline_configured()

        settings = json.loads(path.read_text(encoding="utf-8"))
        assert settings["statusLine"] == original

    _with_temp_home(run)


def test_owned_statusline_can_be_refreshed():
    if ss is None:
        return

    def run(home):
        path = _settings_path(home)
        path.parent.mkdir(parents=True)
        stale = {
            "type": "command",
            "command": "/old/plugin/cache/cognee-memory/0.1.0/scripts/cognee-statusline.sh",
        }
        path.write_text(json.dumps({"statusLine": stale}) + "\n", encoding="utf-8")

        ss._ensure_statusline_configured()

        settings = json.loads(path.read_text(encoding="utf-8"))
        assert settings["statusLine"] != stale
        assert "cognee-statusline.sh" in settings["statusLine"]["command"]

    _with_temp_home(run)


def test_current_statusline_is_not_rewritten():
    if ss is None:
        return

    def run(home):
        path = _settings_path(home)
        path.parent.mkdir(parents=True)
        script = _SCRIPTS / "cognee-statusline.sh"
        desired = {
            "type": "command",
            "command": f'[ -x "{script}" ] && exec "{script}" || true',
        }
        original = json.dumps({"statusLine": desired})
        path.write_text(original, encoding="utf-8")

        ss._ensure_statusline_configured()

        assert path.read_text(encoding="utf-8") == original

    _with_temp_home(run)


def test_corrupt_settings_are_preserved():
    if ss is None:
        return

    def run(home):
        path = _settings_path(home)
        path.parent.mkdir(parents=True)
        original = "{not valid json"
        path.write_text(original, encoding="utf-8")

        ss._ensure_statusline_configured()

        assert path.read_text(encoding="utf-8") == original

    _with_temp_home(run)


def test_statusline_setup_can_be_disabled_by_env():
    if ss is None:
        return

    def run(home):
        os.environ["COGNEE_STATUSLINE"] = "false"
        ss._ensure_statusline_configured()
        assert not _settings_path(home).exists()

    _with_temp_home(run)


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
