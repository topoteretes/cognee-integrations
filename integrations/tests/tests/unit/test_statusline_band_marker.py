"""The settings-file status line steps aside for a session whose row the hooks
module draws above the prompt, and keeps the module's data current.

The module starts no program and writes no file. Contract:
  * it records in its own store (``band:<session_id>``) that it draws the row;
    the text renderer then prints nothing for that session (an empty status
    line is no line), so the row is not shown twice. A marker file from an
    older module (``band/<session_id>.json``) is honoured too;
  * only that session's entry counts: another session in another terminal,
    without the module, keeps its line;
  * a session id from stdin never builds a path unchecked;
  * the renderer saves the row's data in ``status/<session_id>.json``, which is
    what the module reads, including which purple fits the terminal's theme;
  * the ``--json`` answer itself is unaffected by the marker.

claude-code only: codex has no hooks module and no band.
"""

from __future__ import annotations

import io
import json
import time

import pytest
from utils.statusline import write_json

_SESSION = "fde122ae-07db-431d-b5af-acba353e4e3e"
_OTHER = "0b1f8c3e-5d2a-4b7c-9e1f-2a3b4c5d6e7f"


@pytest.fixture
def sl(suite, statusline, temp_home, monkeypatch):
    if not hasattr(statusline, "_band_draws_row"):
        pytest.skip(f"{suite.name}: no hooks module, so no band marker")
    write_json(
        temp_home / ".claude" / "settings.json", {"enabledPlugins": {"cognee-memory@cognee": True}}
    )
    return statusline


def _render(sl, monkeypatch, capsys, session: str, *args: str) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": session})))
    monkeypatch.setattr("sys.argv", ["cognee_statusline_render.py", *args])
    sl.main()
    return capsys.readouterr().out


def _mark(sl, session: str) -> None:
    write_json(
        sl._BAND_DIR / f"{session}.json",
        {"session_id": session, "at": time.time(), "plugin": "cognee-memory"},
    )


def test_without_a_marker_the_line_renders(sl, monkeypatch, capsys):
    # The default style is the band-style line ("● cognee <dataset>"); classic says "cognee:".
    assert "cognee" in _render(sl, monkeypatch, capsys, _SESSION)


def test_with_this_sessions_marker_the_line_is_empty(sl, monkeypatch, capsys):
    _mark(sl, _SESSION)
    assert _render(sl, monkeypatch, capsys, _SESSION) == ""


def test_another_sessions_marker_leaves_this_line_alone(sl, monkeypatch, capsys):
    _mark(sl, _OTHER)
    assert "cognee" in _render(sl, monkeypatch, capsys, _SESSION)


def test_the_json_mode_the_module_uses_is_unaffected(sl, monkeypatch, capsys):
    _mark(sl, _SESSION)
    out = json.loads(_render(sl, monkeypatch, capsys, _SESSION, "--json"))
    assert out["enabled"] is True
    assert "cognee:" in out["text"]


def test_a_session_id_that_is_not_a_safe_path_never_matches(sl):
    assert sl._band_draws_row("../../etc/passwd") is False
    assert sl._band_draws_row("") is False


def test_the_exhausted_preview_switch_renders_the_segment_without_a_marker(sl, monkeypatch):
    monkeypatch.setenv("COGNEE_STATUSLINE_CREDITS", "exhausted")
    seg = sl._credits_segment()
    assert "credits: -$0.40" in seg and "top up: " in seg
    snap = sl._snapshot(_SESSION, enabled=True)
    assert snap["credits_exhausted"] is True
    assert snap["billing_url"] and snap["billing_url"] in snap["credits"]


def _ask(sl, monkeypatch, capsys, payload: dict) -> dict:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr("sys.argv", ["cognee_statusline_render.py", "--json"])
    sl.main()
    return json.loads(capsys.readouterr().out)


def _store(monkeypatch, tmp_path, values: dict) -> None:
    """The hooks module's store, as Claude Code keeps it under its config dir."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    write_json(
        tmp_path / "claude-config" / "plugins" / "store" / "cognee-memory_cognee-0000.json", values
    )


def test_the_modules_store_entry_makes_the_line_step_aside(sl, monkeypatch, capsys, tmp_path):
    assert "cognee" in _render(sl, monkeypatch, capsys, _SESSION)
    _store(monkeypatch, tmp_path, {"band:" + _SESSION: {"at": time.time()}})
    # The status line below steps aside for this session, and only this one.
    assert _render(sl, monkeypatch, capsys, _SESSION) == ""
    assert "cognee" in _render(sl, monkeypatch, capsys, _OTHER)


def test_without_the_store_entry_the_line_comes_back(sl, monkeypatch, capsys, tmp_path):
    _store(
        monkeypatch, tmp_path, {"band:" + _OTHER: {"at": time.time()}, "pick:x": {"target": "d"}}
    )
    assert "cognee" in _render(sl, monkeypatch, capsys, _SESSION)


@pytest.mark.parametrize("band", [None, "true", 1])
def test_a_call_without_a_boolean_band_changes_nothing(sl, monkeypatch, capsys, band):
    payload = {"session_id": _SESSION}
    if band is not None:
        payload["band"] = band
    _ask(sl, monkeypatch, capsys, payload)
    assert not (sl._BAND_DIR / f"{_SESSION}.json").exists()
    _mark(sl, _SESSION)
    _ask(sl, monkeypatch, capsys, payload)
    assert (sl._BAND_DIR / f"{_SESSION}.json").exists()


def test_an_unsafe_session_id_writes_no_marker(sl, monkeypatch, capsys):
    _ask(sl, monkeypatch, capsys, {"session_id": "../../escape", "band": True})
    assert not sl._BAND_DIR.exists() or not any(sl._BAND_DIR.iterdir())


# -- the renderer keeps the data the module draws from


def test_the_renderer_saves_the_rows_data_for_the_module(sl):
    sl.write_status(_SESSION)
    saved = json.loads((sl._STATUS_DIR / f"{_SESSION}.json").read_text(encoding="utf-8"))
    assert saved["enabled"] is True
    assert isinstance(saved["light"], bool)
    assert time.time() - saved["at"] < 60


def test_an_unsafe_session_id_saves_no_data(sl):
    sl.write_status("../" + _SESSION)
    sl.write_status("")
    assert not sl._STATUS_DIR.exists() or not any(sl._STATUS_DIR.iterdir())


def test_every_run_as_the_status_line_refreshes_that_data(sl, monkeypatch, capsys):
    _render(sl, monkeypatch, capsys, _SESSION)
    assert (sl._STATUS_DIR / f"{_SESSION}.json").is_file()


def test_the_modules_logo_is_the_asset(sl):
    """The module holds the mark as text; it must stay the drawing in assets/."""
    plugin = sl._SCRIPTS_DIR.parent if hasattr(sl, "_SCRIPTS_DIR") else None
    from pathlib import Path

    plugin = plugin or Path(sl.__file__).resolve().parent.parent
    asset = (plugin / "assets" / "icon-lavender.svg").read_text(encoding="utf-8").strip()
    module = (plugin / "hooks" / "register.ts").read_text(encoding="utf-8")
    assert "const WAVE_SVG = `" + asset + "`" in module


@pytest.mark.parametrize(
    ("forced", "theme", "light"),
    [
        ("light", "dark", True),
        ("dark", "light", False),
        ("", "light-daltonized", True),
        ("", "", False),
    ],
)
def test_the_json_answer_says_which_purple_the_module_should_use(
    sl, temp_home, monkeypatch, capsys, forced, theme, light
):
    """The module reads neither the override nor Claude Code's settings itself."""
    settings = {"enabledPlugins": {"cognee-memory@cognee": True}}
    if theme:
        settings["theme"] = theme
    write_json(temp_home / ".claude" / "settings.json", settings)
    if forced:
        monkeypatch.setenv("COGNEE_STATUSLINE_THEME", forced)
    else:
        monkeypatch.delenv("COGNEE_STATUSLINE_THEME", raising=False)
    assert _ask(sl, monkeypatch, capsys, {"session_id": _SESSION})["light"] is light


def test_the_module_starts_no_program_and_writes_no_file(sl):
    """It draws from files the scripts keep and from its own store, nothing more."""
    from pathlib import Path

    module = (Path(sl.__file__).resolve().parent.parent / "hooks" / "register.ts").read_text(
        encoding="utf-8"
    )
    for call in ("$.process", "process.run", "$.fs.write", "$.config.", "$.net", "fetch("):
        assert call not in module, call
