"""A mid-session COGNEE_BASE_URL change is detected and reported once (#262).

Hooks re-read ``~/.cognee/.env`` in every process, so an edit made during a
session reaches later hooks while the session stays registered on the server
SessionStart connected to. SessionStart records that server on the launch
record; the per-prompt recall hook compares against it and, once per changed
URL, tells the user and the model that a new session applies the change.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch, tmp_path):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    monkeypatch.delenv("COGNEE_LOCAL_API_URL", raising=False)
    monkeypatch.setenv("COGNEE_SESSION_KEY", "host-1")
    common.ensure_launch_record("host-1", str(tmp_path))
    return common


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("localhost:8011", "http://127.0.0.1:8011/"),
        ("http://LOCALHOST:8011", "http://0.0.0.0:8011"),
        ("https://api.cognee.ai", "https://api.cognee.ai:443/"),
    ],
)
def test_url_identity_ignores_spelling(pc, a, b):
    assert pc._url_identity(a) == pc._url_identity(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("http://localhost:8011", "http://localhost:8012"),
        ("http://localhost:8011", "https://api.cognee.ai"),
        ("https://a.cognee.ai", "https://b.cognee.ai"),
    ],
)
def test_url_identity_tells_servers_apart(pc, a, b):
    assert pc._url_identity(a) != pc._url_identity(b)


def test_unchanged_url_gives_no_notice(pc, monkeypatch):
    pc.set_launch_base_url("host-1", "http://localhost:8011")
    monkeypatch.setenv("COGNEE_BASE_URL", "localhost:8011")
    assert pc.base_url_change_notice() == ""


def test_no_recorded_url_gives_no_notice(pc, monkeypatch):
    """A launch from before this change has no base_url: stay quiet."""
    monkeypatch.setenv("COGNEE_BASE_URL", "https://api.cognee.ai")
    assert pc.base_url_change_notice() == ""


def test_changed_url_is_reported_once_per_url(pc, monkeypatch):
    pc.set_launch_base_url("host-1", "http://localhost:8011")
    monkeypatch.setenv("COGNEE_BASE_URL", "https://api.cognee.ai")

    first = pc.base_url_change_notice()
    assert "http://localhost:8011 -> https://api.cognee.ai" in first
    assert "start a new session" in first
    assert pc.base_url_change_notice() == ""

    monkeypatch.setenv("COGNEE_BASE_URL", "https://other.cognee.ai")
    assert "https://other.cognee.ai" in pc.base_url_change_notice()


def test_a_new_session_start_clears_the_report(pc, monkeypatch):
    pc.set_launch_base_url("host-1", "http://localhost:8011")
    monkeypatch.setenv("COGNEE_BASE_URL", "https://api.cognee.ai")
    assert pc.base_url_change_notice()

    pc.set_launch_base_url("host-1", "https://api.cognee.ai")
    rec = pc._read_map_record("host-1")
    assert rec["base_url"] == "https://api.cognee.ai"
    assert "base_url_notice" not in rec
    assert pc.base_url_change_notice() == ""


def test_recording_needs_an_existing_launch_record(pc):
    pc.set_launch_base_url("other-host", "http://localhost:8011")
    assert pc._read_map_record("other-host") == {}


def test_notice_reaches_the_user_and_the_model(pc, monkeypatch):
    pc.set_launch_base_url("host-1", "http://localhost:8011")
    monkeypatch.setenv("COGNEE_BASE_URL", "https://api.cognee.ai")
    output = {
        "systemMessage": "recall: 2 hits",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "<memory>",
        },
    }
    result = pc.with_base_url_notice(output, "UserPromptSubmit")
    assert result["systemMessage"].startswith("Cognee: COGNEE_BASE_URL changed")
    assert result["systemMessage"].endswith("recall: 2 hits")
    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("Cognee: COGNEE_BASE_URL changed")
    assert context.endswith("<memory>")


def test_notice_creates_output_when_the_hook_had_none(pc, monkeypatch):
    pc.set_launch_base_url("host-1", "http://localhost:8011")
    monkeypatch.setenv("COGNEE_BASE_URL", "https://api.cognee.ai")
    result = pc.with_base_url_notice(None, "UserPromptSubmit")
    assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "COGNEE_BASE_URL changed" in result["systemMessage"]


def test_unchanged_output_passes_through(pc, monkeypatch):
    pc.set_launch_base_url("host-1", "http://localhost:8011")
    monkeypatch.setenv("COGNEE_BASE_URL", "http://localhost:8011")
    output = {"systemMessage": "recall: 0 hits"}
    assert pc.with_base_url_notice(output, "UserPromptSubmit") is output
    assert pc.with_base_url_notice(None, "UserPromptSubmit") is None


def test_recall_hook_shows_the_notice_end_to_end(
    suite, isolated_modules, temp_home, payloads, closed_port_url, monkeypatch
):
    """The real hook, as a subprocess: launch recorded on one server, the env
    now points at another (here: nothing listening), and the notice is shown."""
    import json

    from utils.isolation import run_hook

    common = isolated_modules(suite, "_plugin_common")
    prompt = payloads.user_prompt(prompt="what did we decide about retries?")
    host_key = common._sanitize_session_key(prompt["session_id"])
    common.ensure_launch_record(host_key, str(temp_home))
    common.set_launch_base_url(host_key, "http://localhost:8011")

    result = run_hook(
        suite,
        "session-context-lookup.py",
        stdin=prompt,
        home=temp_home,
        service_url=closed_port_url,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert "COGNEE_BASE_URL changed" in output["systemMessage"]
    assert closed_port_url in output["systemMessage"]
