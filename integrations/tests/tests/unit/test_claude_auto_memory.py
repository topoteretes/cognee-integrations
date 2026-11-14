"""Claude Code's native auto-memory: advised about, never written (SDK-1411)."""

import json

import pytest


@pytest.fixture
def am(suite, isolated_modules):
    if suite.name != "claude-code":
        pytest.skip("Claude settings")
    return isolated_modules(suite, "_auto_memory")


def _settings(am):
    return am._paths()[0]


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_cloud_advises_once_and_never_writes_settings(am):
    settings = _settings(am)
    _write(settings, {"theme": "dark"})
    before = settings.read_bytes()
    first = am.advice(True)
    assert "auto-memory is on alongside Cognee" in first
    assert am.advice(True) == ""
    assert settings.read_bytes() == before


def test_no_settings_file_is_never_created(am):
    assert "alongside Cognee" in am.advice(True)
    assert not _settings(am).exists()


def test_local_mode_says_nothing(am):
    _write(_settings(am), {})
    assert am.advice(False) == ""


def test_user_already_turned_it_off(am):
    _write(_settings(am), {"autoMemoryEnabled": False})
    assert am.advice(True) == ""


def test_an_earlier_versions_change_is_reported_not_undone(am):
    settings, marker = am._paths()
    _write(settings, {"autoMemoryEnabled": False})
    _write(marker, {"owned": True, "previous_present": False, "previous_value": None})
    before = settings.read_bytes()
    notice = am.advice(False)
    assert "earlier Cognee version turned off Claude auto-memory" in notice
    assert am.advice(False) == ""
    assert settings.read_bytes() == before


def test_corrupt_settings_are_left_alone(am):
    settings = _settings(am)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{not json", encoding="utf-8")
    assert am.advice(True) == ""
    assert settings.read_text(encoding="utf-8") == "{not json"


def test_state_is_read_only(am):
    _write(_settings(am), {"autoMemoryEnabled": True})
    state = am.state()
    assert state["enabled"] is True and state["plugin_owned"] is False
