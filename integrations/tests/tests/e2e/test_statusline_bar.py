"""The status-line bar rendered by the real entrypoint, as a subprocess.

``cognee-statusline.sh`` runs ``cognee_statusline_render.py`` directly, so this
runs the same way and asserts the whole line — the one place where glyph
placement, the mode word and segment order are checked together rather than
per-function.

Per-suite expectations are genuine: claude-code styles the mode word for a
terminal and self-evicts unless the plugin is enabled in
``~/.claude/settings.json``; codex emits plain text (its bar is injected into the
model's context) and has no plugin-enablement gate.

Migrated from the full-render tests in
claude-code/tests/test_statusline_{llm_state,mode_style}.py and
codex/tests/test_statusline_plain_text.py.
"""

from __future__ import annotations

import json
import subprocess
import time

from utils.statusline import mode_label, write_json

_LOCAL_URL = "http://127.0.0.1:8000"
_CLOUD_URL = "https://api.example-cognee.ai"


def _enable_plugin(suite, temp_home):
    """claude-code's renderer self-evicts unless the plugin is enabled."""
    if suite.name != "claude-code":
        return
    write_json(
        temp_home / ".claude" / "settings.json",
        {"enabledPlugins": {"cognee-memory@cognee": True}},
    )


def _render(suite, run_hook, base_url, stdin="{}"):
    return run_hook(
        suite,
        "cognee_statusline_render.py",
        stdin=stdin,
        service_url=base_url,
        api_key=None,
    )


def test_bar_shows_dataset_and_mode(suite, run_hook, statusline, temp_home):
    """The baseline line: no failures anywhere, so no glyph — just the facts."""
    _enable_plugin(suite, temp_home)
    result = _render(suite, run_hook, _CLOUD_URL, stdin=json.dumps({"session_id": "s1"}))
    assert result.returncode == 0, result.stderr

    expected_mode = mode_label(statusline, "cloud")
    assert result.stdout == f"cognee: {suite.default_dataset} · {expected_mode}", repr(
        result.stdout
    )


def test_bar_prefers_pinned_dataset(suite, run_hook, temp_home):
    _enable_plugin(suite, temp_home)
    sessions = temp_home / ".cognee-plugin" / suite.state_subdir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "host-one.json").write_text(
        json.dumps({"session_id": "sid", "dataset": "project_repo_111111111111"}),
        encoding="utf-8",
    )
    result = run_hook(
        suite,
        "cognee_statusline_render.py",
        stdin={"session_id": "host-one", "cwd": "/wrong/project"},
        service_url="https://api.example-cognee.ai",
        api_key=None,
        env={"COGNEE_PLUGIN_DATASET": "explicit-env"},
    )
    assert "cognee: project_repo_111111111111" in result.stdout


def test_bar_normalizes_host_id_before_reading_pinned_dataset(
    suite, run_hook, temp_home
):
    _enable_plugin(suite, temp_home)
    host_id = "._host/id with spaces?" + "x" * 140 + "..."
    normalized_host_id = "host_id_with_spaces_" + "x" * 100
    sessions = temp_home / ".cognee-plugin" / suite.state_subdir / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{normalized_host_id}.json").write_text(
        json.dumps({"session_id": "sid", "dataset": "project_normalized_222222222222"}),
        encoding="utf-8",
    )
    result = run_hook(
        suite,
        "cognee_statusline_render.py",
        stdin={"session_id": host_id, "cwd": "/wrong/project"},
        service_url="https://api.example-cognee.ai",
        api_key=None,
        env={"COGNEE_PLUGIN_DATASET": "explicit-env"},
    )
    assert "cognee: project_normalized_222222222222" in result.stdout


def test_bar_derives_before_launch_record_exists(
    suite, run_hook, temp_home, project_dir
):
    _enable_plugin(suite, temp_home)
    subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:Org/StatusRepo.git"],
        cwd=project_dir,
        check=True,
    )
    result = run_hook(
        suite,
        "cognee_statusline_render.py",
        stdin={"session_id": "new-host", "cwd": str(project_dir)},
        cwd=project_dir,
        service_url="https://api.example-cognee.ai",
        api_key=None,
        env={"COGNEE_DATASET_SCOPE": "project"},
    )
    assert "cognee: project_statusrepo_" in result.stdout


def test_llm_failure_glyph_renders_left_of_the_label(suite, run_hook, statusline, temp_home):
    """A broken LLM key with a healthy server: the sign sits before "cognee:"."""
    _enable_plugin(suite, temp_home)
    # The renderer reads these from the same temp HOME the subprocess gets.
    write_json(
        statusline._LLM_STATE_PATH,
        {"llm_state": "not_set", "checked_at": time.time()},
    )
    write_json(statusline._SERVER_READY_PATH, {"state": "ready", "base_url": _LOCAL_URL})

    result = _render(suite, run_hook, _LOCAL_URL)
    assert result.returncode == 0, result.stderr

    glyph = (
        statusline._fail_glyph(statusline._LLM_KEY_REASON)
        if hasattr(statusline, "_fail_glyph")
        else f"✕ ({statusline._LLM_KEY_REASON}) "
    )
    expected_mode = mode_label(statusline, "local")
    assert result.stdout == f"{glyph}cognee: {suite.default_dataset} · {expected_mode}", repr(
        result.stdout
    )


def test_codex_bar_carries_no_ansi_escapes(suite, run_hook, statusline, temp_home):
    """codex's line is injected into the model's context, so it must stay plain."""
    if hasattr(statusline, "_mode_label"):
        import pytest

        pytest.skip(f"{suite.name}: the bar is deliberately styled for a terminal")

    write_json(statusline._LLM_STATE_PATH, {"llm_state": "not_set", "checked_at": time.time()})
    result = _render(suite, run_hook, _CLOUD_URL)
    assert result.returncode == 0, result.stderr
    assert "\033" not in result.stdout, repr(result.stdout)
