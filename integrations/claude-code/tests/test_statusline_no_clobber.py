"""Tests for `_ensure_statusline_configured` not clobbering a user's status line.

`~/.claude/settings.json` is global, shared Claude Code config. The SessionStart
hook must install Cognee's status line only when none exists, self-heal *its own*
entry across plugin versions (tracked explicitly via statusLineCogneeManaged),
never destroy a user's own custom `statusLine`, fail closed on corrupt JSON, and
honour the reversible `COGNEE_FORCE_STATUSLINE` override.

Run: python integrations/claude-code/tests/test_statusline_no_clobber.py
"""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

# session-start.py has a hyphen, so it can't be imported by name — load by path.
# Guarded like the sibling tests: if the hook's deps aren't importable in this
# environment, skip rather than erroring at collection time.
try:
    _spec = importlib.util.spec_from_file_location("session_start", _SCRIPTS / "session-start.py")
    session_start = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(session_start)
except Exception as exc:  # pragma: no cover - environment-dependent
    print("SKIP test_statusline_no_clobber:", exc)
    session_start = None

MANAGED = "statusLineCogneeManaged"
BACKUP = "statusLineCogneeBackup"
COGNEE_CMD_SUFFIX = "cognee-statusline.sh"
CUSTOM = {"type": "command", "command": "/my/custom/statusline.sh"}
# The exact command the script installs when CLAUDE_PLUGIN_ROOT is unset (local
# fallback): the cognee-statusline.sh next to session-start.py.
EXPECTED_CMD = str((_SCRIPTS / "session-start.py").resolve().parent / "cognee-statusline.sh")


def _settings_path(home):
    return pathlib.Path(home) / ".claude" / "settings.json"


def _write_raw(home, raw_text):
    p = _settings_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(raw_text, encoding="utf-8")


def _run_with_home(tmp_home, initial_settings=None, force=False):
    """Invoke the function with HOME pointed at a temp dir; return the resulting settings dict.

    Neutralizes CLAUDE_PLUGIN_ROOT so the script resolves its status-line path via
    the local fallback (next to the script, which exists) rather than an inherited
    plugin root that would not exist under the temp HOME. When initial_settings is
    None the existing file (if any) is left as-is.
    """
    settings_path = _settings_path(tmp_home)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if initial_settings is not None:
        settings_path.write_text(json.dumps(initial_settings), encoding="utf-8")

    saved = {k: os.environ.get(k) for k in ("HOME", "COGNEE_FORCE_STATUSLINE", "CLAUDE_PLUGIN_ROOT")}
    os.environ["HOME"] = str(tmp_home)
    os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
    if force:
        os.environ["COGNEE_FORCE_STATUSLINE"] = "1"
    else:
        os.environ.pop("COGNEE_FORCE_STATUSLINE", None)
    try:
        session_start._ensure_statusline_configured()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    if settings_path.exists():
        try:
            return json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None  # corrupt file left intact
    return None


def test_installs_when_absent():
    with tempfile.TemporaryDirectory() as home:
        settings = _run_with_home(home, initial_settings={})
        assert settings["statusLine"]["command"].endswith(COGNEE_CMD_SUFFIX)
        assert settings[MANAGED] == settings["statusLine"]["command"]


def test_preserves_user_statusline():
    with tempfile.TemporaryDirectory() as home:
        settings = _run_with_home(home, initial_settings={"statusLine": CUSTOM})
        assert settings["statusLine"] == CUSTOM
        assert MANAGED not in settings and BACKUP not in settings


def test_preserves_user_statusline_with_matching_basename():
    # A user's *own* script that merely shares the basename must not be adopted:
    # ownership is by recorded marker, not by filename.
    lookalike = {"type": "command", "command": "/home/me/bin/cognee-statusline.sh"}
    with tempfile.TemporaryDirectory() as home:
        settings = _run_with_home(home, initial_settings={"statusLine": lookalike})
        assert settings["statusLine"] == lookalike
        assert MANAGED not in settings


def test_noop_when_already_cognee():
    with tempfile.TemporaryDirectory() as home:
        first = _run_with_home(home, initial_settings={})
        second = _run_with_home(home)  # idempotent — no churn
        assert second["statusLine"] == first["statusLine"]
        assert second[MANAGED] == first[MANAGED]
        assert BACKUP not in second


def test_adopts_unmarked_current_install():
    # An install from older code (no marker) whose path already equals the current
    # one is adopted: marker recorded, status line unchanged.
    with tempfile.TemporaryDirectory() as home:
        settings = _run_with_home(home, initial_settings={"statusLine": {"type": "command", "command": EXPECTED_CMD}})
        assert settings["statusLine"]["command"] == EXPECTED_CMD
        assert settings[MANAGED] == EXPECTED_CMD


def test_self_heals_stale_cognee_path_when_marked():
    # A marked Cognee line from an older install (different path) is repathed.
    stale_cmd = "/old/plugin/v0.1.0/scripts/cognee-statusline.sh"
    with tempfile.TemporaryDirectory() as home:
        settings = _run_with_home(
            home,
            initial_settings={"statusLine": {"type": "command", "command": stale_cmd}, MANAGED: stale_cmd},
        )
        assert settings["statusLine"]["command"] == EXPECTED_CMD
        assert settings[MANAGED] == EXPECTED_CMD


def test_unmarked_stale_lookalike_is_preserved():
    # Without a marker, a stale cognee-named line at a different path is treated as
    # the user's and left alone (conservative — never destroys).
    stale = {"type": "command", "command": "/old/plugin/v0.1.0/scripts/cognee-statusline.sh"}
    with tempfile.TemporaryDirectory() as home:
        settings = _run_with_home(home, initial_settings={"statusLine": stale})
        assert settings["statusLine"] == stale
        assert MANAGED not in settings


def test_empty_statusline_is_replaced():
    with tempfile.TemporaryDirectory() as home:
        settings = _run_with_home(home, initial_settings={"statusLine": {}})
        assert settings["statusLine"]["command"].endswith(COGNEE_CMD_SUFFIX)


def test_non_dict_statusline_is_preserved():
    with tempfile.TemporaryDirectory() as home:
        settings = _run_with_home(home, initial_settings={"statusLine": "legacy-string-value"})
        assert settings["statusLine"] == "legacy-string-value"
        assert MANAGED not in settings


def test_corrupt_json_is_left_untouched():
    # Fail closed: an unparseable settings.json must never be overwritten.
    garbage = "{ this is not valid json "
    with tempfile.TemporaryDirectory() as home:
        _write_raw(home, garbage)
        _run_with_home(home)  # initial_settings=None -> don't rewrite our garbage
        assert _settings_path(home).read_text(encoding="utf-8") == garbage


def test_force_override_backs_up_user_statusline():
    with tempfile.TemporaryDirectory() as home:
        settings = _run_with_home(home, initial_settings={"statusLine": CUSTOM}, force=True)
        assert settings["statusLine"]["command"].endswith(COGNEE_CMD_SUFFIX)
        assert settings[BACKUP] == CUSTOM
        assert settings[MANAGED] == settings["statusLine"]["command"]


def test_force_override_is_reversible():
    with tempfile.TemporaryDirectory() as home:
        forced = _run_with_home(home, initial_settings={"statusLine": CUSTOM}, force=True)
        assert forced["statusLine"]["command"].endswith(COGNEE_CMD_SUFFIX)
        restored = _run_with_home(home)  # force unset
        assert restored["statusLine"] == CUSTOM
        assert BACKUP not in restored and MANAGED not in restored


def test_force_backup_keeps_newest_displaced_value():
    # A pre-existing backup must be overwritten with the newly displaced value
    # (proves direct assignment, not setdefault).
    other = {"type": "command", "command": "/another/statusline.sh"}
    stale_backup = {"type": "command", "command": "/stale/old.sh"}
    with tempfile.TemporaryDirectory() as home:
        settings = _run_with_home(
            home, initial_settings={"statusLine": other, BACKUP: stale_backup}, force=True
        )
        assert settings["statusLine"]["command"].endswith(COGNEE_CMD_SUFFIX)
        assert settings[BACKUP] == other  # newest wins, not the stale backup


def test_orphan_backup_is_cleaned_not_resurrected():
    # User took ownership while an orphaned backup/marker lingered: clean them so a
    # later statusLine removal can't resurrect the dead value.
    user_c = {"type": "command", "command": "/user/c.sh"}
    dead_a = {"type": "command", "command": "/dead/a.sh"}
    with tempfile.TemporaryDirectory() as home:
        cleaned = _run_with_home(
            home, initial_settings={"statusLine": user_c, BACKUP: dead_a, MANAGED: EXPECTED_CMD}
        )
        assert cleaned["statusLine"] == user_c
        assert BACKUP not in cleaned and MANAGED not in cleaned
        # Now the user removes their status line entirely: fresh install, not A.
        _write_raw(home, json.dumps({}))
        after = _run_with_home(home)
        assert after["statusLine"]["command"].endswith(COGNEE_CMD_SUFFIX)
        assert after["statusLine"] != dead_a


def test_self_heals_unmarked_cognee_memory_install():
    # A legacy (no-marker) line inside the cognee-memory plugin dir is recognised
    # as ours by path and self-healed to the current install.
    legacy = {
        "type": "command",
        "command": "/u/.claude/plugins/cache/cognee/cognee-memory/0.1.0/scripts/cognee-statusline.sh",
    }
    with tempfile.TemporaryDirectory() as home:
        settings = _run_with_home(home, initial_settings={"statusLine": legacy})
        assert settings["statusLine"]["command"] == EXPECTED_CMD
        assert settings[MANAGED] == EXPECTED_CMD


def test_no_resurrect_after_user_clears_statusline():
    # Force displaces the user's line, then the user unsets the env var AND removes
    # the statusLine entirely (unaware of the backup key). It must NOT reappear.
    with tempfile.TemporaryDirectory() as home:
        forced = _run_with_home(home, initial_settings={"statusLine": CUSTOM}, force=True)
        assert forced[BACKUP] == CUSTOM
        cur = json.loads(_settings_path(home).read_text(encoding="utf-8"))
        cur.pop("statusLine", None)  # user deletes the slot, leaves bookkeeping behind
        _write_raw(home, json.dumps(cur))
        after = _run_with_home(home)  # force off
        assert after["statusLine"]["command"].endswith(COGNEE_CMD_SUFFIX)
        assert after["statusLine"] != CUSTOM
        assert BACKUP not in after


def test_non_object_json_is_left_untouched():
    # Valid JSON that isn't an object must be left alone (fail closed).
    raw = '["not", "an", "object"]'
    with tempfile.TemporaryDirectory() as home:
        _write_raw(home, raw)
        _run_with_home(home)
        assert _settings_path(home).read_text(encoding="utf-8") == raw


def test_symlinked_settings_is_followed_not_replaced():
    # A symlinked settings.json (dotfiles pattern) must be edited through the link,
    # not replaced by a regular file.
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as dots:
        target = pathlib.Path(dots) / "settings.json"
        target.write_text("{}", encoding="utf-8")
        link = _settings_path(home)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        _run_with_home(home)
        assert link.is_symlink()  # link preserved
        assert not target.is_symlink()
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["statusLine"]["command"].endswith(COGNEE_CMD_SUFFIX)


def test_preserves_file_permissions():
    with tempfile.TemporaryDirectory() as home:
        path = _settings_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        os.chmod(path, 0o644)
        _run_with_home(home)  # initial_settings=None: keep the file we set up
        assert (path.stat().st_mode & 0o777) == 0o644


def test_new_file_is_private():
    # A settings.json created from scratch stays owner-only (0600) — it can hold
    # secrets, so creation must not broaden permissions.
    with tempfile.TemporaryDirectory() as home:
        _run_with_home(home)  # no file exists yet
        path = _settings_path(home)
        assert path.exists()
        assert (path.stat().st_mode & 0o777) == 0o600


if __name__ == "__main__":
    if session_start is None:
        print("SKIP: session-start module unavailable")
        sys.exit(0)
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except Exception as exc:  # catch all so one failure doesn't abort the run
                failures += 1
                print("FAIL", _name, repr(exc))
    sys.exit(1 if failures else 0)
