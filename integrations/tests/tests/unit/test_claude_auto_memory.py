"""Native-memory settings ownership and restoration."""

import json
import os

import pytest


@pytest.fixture
def am(suite, isolated_modules):
    if suite.name != "claude-code":
        pytest.skip("Claude settings")
    return isolated_modules(suite, "_auto_memory")


def test_set_once_restore_missing_value(am, monkeypatch):
    settings, marker = am._paths()
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"statusLine": "mine"}))
    assert "next session" in am.configure()
    assert am.configure() == ""
    assert am.state()["plugin_owned"]
    assert json.loads(settings.read_text()) == {"statusLine": "mine", "autoMemoryEnabled": False}
    monkeypatch.setenv("COGNEE_DISABLE_AUTO_MEMORY", "false")
    assert "restored" in am.configure()
    assert json.loads(settings.read_text()) == {"statusLine": "mine"}
    assert not marker.exists()


def test_explicit_true_is_respected_and_reported_once(am):
    settings, _ = am._paths()
    settings.parent.mkdir(parents=True)
    settings.write_text('{"autoMemoryEnabled":true}')
    assert "unchanged" in am.configure()
    assert am.configure() == ""
    assert settings.read_text() == '{"autoMemoryEnabled":true}'
    assert not am.restore()


def test_preexisting_false_is_not_owned(am):
    settings, marker = am._paths()
    settings.parent.mkdir(parents=True)
    settings.write_text('{"autoMemoryEnabled":false}')
    assert am.configure() == ""
    assert not marker.exists()
    assert not am.restore()


def test_user_edit_after_plugin_write_survives_cleanup(am):
    am.configure()
    settings, marker = am._paths()
    settings.write_text('{"autoMemoryEnabled":true,"theme":"dark"}')
    assert am.restore()
    assert json.loads(settings.read_text()) == {"autoMemoryEnabled": True, "theme": "dark"}
    assert not marker.exists()


def test_corrupt_settings_preserved(am):
    settings, marker = am._paths()
    settings.parent.mkdir(parents=True)
    settings.write_text("{bad")
    with pytest.raises(ValueError):
        am.configure()
    assert settings.read_text() == "{bad"
    assert not marker.exists()


def test_renderer_cleanup_restores_setting(am, suite, isolated_modules):
    am.configure()
    renderer = isolated_modules(suite, "cognee_statusline_render")
    renderer._evict_own_statusline()
    assert am.state()["enabled"] == "default"


def test_settings_keep_formatting_permissions_and_symlink(am, tmp_path):
    settings, _ = am._paths()
    settings.parent.mkdir(parents=True)
    real = tmp_path / "dotfiles" / "claude-settings.json"
    real.parent.mkdir()
    real.write_text('{\n    "theme": "dark"\n}\n')
    real.chmod(0o644)
    try:
        settings.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    am.configure()
    assert settings.is_symlink() and settings.resolve() == real.resolve()
    assert real.read_text() == '{\n    "theme": "dark",\n    "autoMemoryEnabled": false\n}\n'
    if os.name != "nt":
        assert real.stat().st_mode & 0o777 == 0o644
    assert am.restore()
    assert real.read_text() == '{\n    "theme": "dark"\n}\n'


def test_single_line_settings_stay_single_line(am):
    settings, _ = am._paths()
    settings.parent.mkdir(parents=True)
    settings.write_text('{"theme":"dark"}')
    am.configure()
    assert settings.read_text() == '{"theme": "dark", "autoMemoryEnabled": false}'


def test_user_removing_the_key_is_respected(am):
    settings, _ = am._paths()
    settings.parent.mkdir(parents=True)
    settings.write_text('{"theme": "dark"}')
    assert "next session" in am.configure()
    settings.write_text('{"theme": "dark"}')  # the user deletes our setting
    assert am.configure() == ""
    assert am.configure() == ""
    assert json.loads(settings.read_text()) == {"theme": "dark"}
    assert not am.restore()


def test_restore_without_marker_leaves_claude_dir_alone(am):
    settings, _ = am._paths()
    assert not am.restore()
    assert not settings.parent.exists()


def test_local_mode_gives_the_setting_back(am):
    settings, marker = am._paths()
    settings.parent.mkdir(parents=True)
    settings.write_text('{"theme": "dark"}')
    assert "next session" in am.sync(cloud=True)
    assert "restored" in am.sync(cloud=False)
    assert json.loads(settings.read_text()) == {"theme": "dark"}
    assert not marker.exists()
    assert am.sync(cloud=False) == ""
