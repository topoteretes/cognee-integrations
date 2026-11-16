"""The settings-file status line steps aside for a session whose row the hooks
module draws above the prompt.

Contract:
  * the module writes no file. It passes ``band`` with each ``--json`` call,
    and the renderer keeps ``band/<session_id>.json``: ``true`` writes it,
    ``false`` removes it, and a call without the field changes nothing;
  * the text renderer prints nothing for a session with the marker (an empty
    status line is no line), so the row is not shown twice;
  * only that session's marker counts: another session in another terminal,
    without the module, keeps its line;
  * a session id from stdin never builds a path unchecked;
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


def test_the_module_asking_with_band_on_writes_the_marker(sl, monkeypatch, capsys):
    marker = sl._BAND_DIR / f"{_SESSION}.json"
    assert not marker.exists()
    out = _ask(sl, monkeypatch, capsys, {"session_id": _SESSION, "band": True})
    assert out["enabled"] is True
    assert json.loads(marker.read_text())["session_id"] == _SESSION
    # The status line below now steps aside for this session, and only this one.
    assert _render(sl, monkeypatch, capsys, _SESSION) == ""
    assert "cognee" in _render(sl, monkeypatch, capsys, _OTHER)


def test_band_off_removes_the_marker_so_the_line_comes_back(sl, monkeypatch, capsys):
    _mark(sl, _SESSION)
    _ask(sl, monkeypatch, capsys, {"session_id": _SESSION, "band": False})
    assert not (sl._BAND_DIR / f"{_SESSION}.json").exists()
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
