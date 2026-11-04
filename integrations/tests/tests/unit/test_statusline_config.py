"""Tests for `_ensure_statusline_configured` (session-start.py) — the write of
the `statusLine` entry into the host's `~/.claude/settings.json`.

One row, whichever can be drawn: in a session whose row the hooks module draws
above the prompt (its `band/<session>.json` marker exists) the plugin's own
entry is removed, since Claude Code keeps the status-line row reserved as long
as the entry exists (a blank row under the prompt otherwise); otherwise the
line below the prompt is installed. Beyond that, the plugin owns exactly what
it wrote and nothing else:
  * absent entry → write ours;
  * anything the user put there (dict, bare string, a similarly-named command, or
    a cognee mention outside the command itself) → left untouched;
  * an entry that IS ours but points at a stale plugin-cache path → refreshed;
  * an entry already current (refreshInterval included) → not rewritten at all,
    byte for byte;
  * corrupt JSON → preserved, never clobbered;
  * `COGNEE_STATUSLINE=false` → nothing written.

claude-code only: codex has no `_ensure_statusline_configured` (its host wires
the status line itself).

Migrated from claude-code/tests/test_statusline_config.py, whose `ss is None`
guard silently reported skipped tests as PASS — a real skip now.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def ss(suite, hook_module, temp_home, monkeypatch):
    module = hook_module(suite, "session-start.py")
    if not hasattr(module, "_ensure_statusline_configured"):
        pytest.skip(f"{suite.name}: the host wires its own status line")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    return module


@pytest.fixture
def settings(temp_home):
    """The host settings file the writer targets, inside the per-test HOME."""
    return temp_home / ".claude" / "settings.json"


def _write(settings, payload, *, raw=False):
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(payload if raw else json.dumps(payload), encoding="utf-8")


def _status_line(settings):
    return json.loads(settings.read_text(encoding="utf-8"))["statusLine"]


def test_statusline_written_when_absent(ss, settings):
    ss._ensure_statusline_configured()
    assert "cognee-statusline.sh" in _status_line(settings)["command"]


def test_existing_user_statusline_is_preserved(ss, settings):
    original = {"type": "command", "command": "printf custom-status"}
    _write(settings, {"statusLine": original})
    ss._ensure_statusline_configured()
    assert _status_line(settings) == original


def test_existing_non_dict_statusline_is_preserved(ss, settings):
    original = "/my/custom/statusline.sh"
    _write(settings, {"statusLine": original})
    ss._ensure_statusline_configured()
    assert _status_line(settings) == original


def test_cognee_marker_outside_command_does_not_claim_statusline(ss, settings):
    original = {
        "type": "command",
        "command": "printf custom-status",
        "description": "not the cognee-statusline command",
    }
    _write(settings, {"statusLine": original})
    ss._ensure_statusline_configured()
    assert _status_line(settings) == original


def test_similarly_named_custom_statusline_is_preserved(ss, settings):
    original = {"type": "command", "command": "/usr/local/bin/cognee-statusline-custom"}
    _write(settings, {"statusLine": original})
    ss._ensure_statusline_configured()
    assert _status_line(settings) == original


def test_owned_statusline_can_be_refreshed(ss, settings):
    stale = {
        "type": "command",
        "command": "/old/plugin/cache/cognee-memory/0.1.0/scripts/cognee-statusline.sh",
    }
    _write(settings, {"statusLine": stale})
    ss._ensure_statusline_configured()
    refreshed = _status_line(settings)
    assert refreshed != stale
    assert "cognee-statusline.sh" in refreshed["command"]


def test_current_statusline_is_not_rewritten(ss, suite, settings):
    script = suite.scripts_dir / "cognee-statusline.sh"
    # Must match what _ensure_statusline_configured() considers current —
    # refreshInterval included, or the entry reads as outdated and the "no
    # rewrite" path under test is never taken.
    desired = {
        "type": "command",
        "command": f'[ -x "{script}" ] && exec "{script}" || true',
        "refreshInterval": 2,
    }
    original = json.dumps({"statusLine": desired})
    _write(settings, original, raw=True)
    ss._ensure_statusline_configured()
    assert settings.read_text(encoding="utf-8") == original


def test_corrupt_settings_are_preserved(ss, settings):
    original = "{not valid json"
    _write(settings, original, raw=True)
    ss._ensure_statusline_configured()
    assert settings.read_text(encoding="utf-8") == original


def test_statusline_setup_can_be_disabled_by_env(ss, settings, monkeypatch):
    monkeypatch.setenv("COGNEE_STATUSLINE", "false")
    ss._ensure_statusline_configured()
    assert not settings.exists()


# ── the gate: the module's row or the line, never both, never neither ────────

SESSION = "11111111-2222-4333-8444-555555555555"


def _mark_band(temp_home, session=SESSION):
    """What the hooks module writes at a session start it runs in."""
    band = temp_home / ".cognee-plugin" / "claude-code" / "band"
    band.mkdir(parents=True, exist_ok=True)
    (band / f"{session}.json").write_text(json.dumps({"session_id": session}), encoding="utf-8")


def test_nothing_is_written_for_a_session_the_module_draws(ss, settings, temp_home):
    _mark_band(temp_home)
    ss._ensure_statusline_configured(SESSION)
    assert not settings.exists()


def test_our_own_entry_is_removed_for_a_session_the_module_draws(ss, settings, temp_home):
    """An entry from before the module keeps a blank status-line row reserved
    under the prompt (the renderer prints nothing for a band session), so it
    goes; the rest of the settings file is untouched."""
    _mark_band(temp_home)
    ours = {"type": "command", "command": "/old/cognee-statusline.sh", "refreshInterval": 2}
    _write(settings, {"statusLine": ours, "theme": "dark"})
    ss._ensure_statusline_configured(SESSION)
    written = json.loads(settings.read_text(encoding="utf-8"))
    assert "statusLine" not in written
    assert written["theme"] == "dark"


def test_a_user_entry_is_kept_for_a_session_the_module_draws(ss, settings, temp_home):
    _mark_band(temp_home)
    theirs = {"type": "command", "command": "~/.claude/statusline-chain.sh"}
    _write(settings, {"statusLine": theirs})
    ss._ensure_statusline_configured(SESSION)
    assert _status_line(settings) == theirs


def test_no_settings_file_is_created_by_the_eviction(ss, settings, temp_home):
    _mark_band(temp_home)
    ss._ensure_statusline_configured(SESSION)
    assert not settings.exists()


def test_corrupt_settings_survive_the_eviction(ss, settings, temp_home):
    _mark_band(temp_home)
    _write(settings, "{not json", raw=True)
    ss._ensure_statusline_configured(SESSION)
    assert settings.read_text(encoding="utf-8") == "{not json"


def test_the_line_is_installed_for_a_session_the_module_does_not_draw(ss, settings, temp_home):
    """Another session's marker (the Desktop app) says nothing about this one:
    an older CLI or a --safe-mode launch on the same machine keeps its line."""
    _mark_band(temp_home, session="other-session")
    ss._ensure_statusline_configured(SESSION)
    assert "cognee-statusline.sh" in _status_line(settings)["command"]


def test_the_line_is_installed_when_no_session_id_is_known(ss, settings):
    ss._ensure_statusline_configured("")
    assert "cognee-statusline.sh" in _status_line(settings)["command"]
