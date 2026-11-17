"""Local mode with no Python 3.10+ for the Cognee server is told to the user, by cause.

The plugin never downloads an interpreter, so a machine with only an old python3
(macOS's 3.9.6) cannot run the local server. That must surface as "Cognee needs
Python 3.10" — not as a generic ``unreachable`` — in every place the user looks:
the session-start message, the status line and ``doctor.py``. Asserted for every
suite; the three carry the same bootstrap.
"""

from __future__ import annotations

import json
import sys

import pytest
from utils.suites import plugin_root

_MARKER_NAME = "host-python-unsupported.json"


@pytest.fixture
def ss(suite, hook_module, monkeypatch):
    module = hook_module(suite, "session-start.py")
    monkeypatch.setattr(module, "_find_uv", lambda: "")
    monkeypatch.setattr(module.shutil, "which", lambda name, *a, **k: None)
    return module


# --- 1. the message names Cognee's requirement, the fix, and the cloud exemption ----


def test_refusal_message_names_cognee_requirement(suite, ss, temp_home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "version_info", (3, 9, 6, "final", 0))
    assert ss.ensure_cognee_installed() is False
    err = capsys.readouterr().err
    assert "Cognee requires Python 3.10 or newer" in err
    assert "up to 3.14" in err
    assert "does not download" in err
    assert "Cloud mode" in err
    assert "python3.12" in err  # what was checked, so the user knows which name to provide
    # The same text reaches the next SessionStart via the marker.
    recorded = json.loads((plugin_root(temp_home) / _MARKER_NAME).read_text(encoding="utf-8"))
    assert "Cognee requires Python 3.10 or newer" in recorded["message"]
    assert ss._apply_host_python_warning({})["systemMessage"] == recorded["message"]


# --- 2. the status line shows the cause, in local mode only ------------------------


def _write_marker(temp_home):
    marker = plugin_root(temp_home) / _MARKER_NAME
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"message": "Cognee requires Python 3.10 or newer"}), "utf-8")
    return marker


def test_statusline_shows_python_reason_in_local_mode(statusline, temp_home):
    _write_marker(temp_home)
    prefix = statusline._status_prefix("sess-1")
    assert "✕" in prefix
    assert "cognee_needs_python_3_10" in prefix


def test_statusline_python_reason_clears_with_the_marker(statusline, temp_home):
    marker = _write_marker(temp_home)
    assert "cognee_needs_python_3_10" in statusline._status_prefix("sess-1")
    marker.unlink()  # what _write_venv_ready does once a venv exists
    assert "cognee_needs_python_3_10" not in statusline._status_prefix("sess-1")


def test_statusline_python_reason_not_shown_in_cloud_mode(statusline, temp_home, monkeypatch):
    """The marker may linger from a local attempt; a cloud session needs no runtime."""
    _write_marker(temp_home)
    monkeypatch.setenv("COGNEE_BASE_URL", "https://api.cognee.ai")
    assert statusline._active_mode() == "cloud"
    assert "cognee_needs_python_3_10" not in statusline._status_prefix("sess-1")


def test_statusline_missing_url_still_wins_over_python(statusline, temp_home, monkeypatch):
    _write_marker(temp_home)
    monkeypatch.setenv("COGNEE_BACKEND", "cloud")
    prefix = statusline._status_prefix("sess-1")
    assert "missing_cognee_base_url" in prefix
    assert "cognee_needs_python_3_10" not in prefix


# --- 3. doctor: a Runtime Python row that says MISSING and why ---------------------


@pytest.fixture
def doctor(suite, isolated_modules):
    return isolated_modules(suite, "doctor")


def test_doctor_reports_missing_runtime_after_refusal(doctor, temp_home):
    _write_marker(temp_home)
    value = doctor._resolve_runtime_python()
    assert value.startswith("MISSING")
    assert "Cognee needs Python 3.10-3.14" in value


def test_doctor_reports_missing_runtime_when_no_interpreter_found(doctor, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(doctor, "_python_version_of", lambda python: ())
    value = doctor._resolve_runtime_python()
    assert value.startswith("MISSING")
    assert "none found on PATH" in value


def test_doctor_reports_the_interpreter_the_next_start_will_use(doctor, monkeypatch):
    import shutil

    fake = "/opt/homebrew/bin/python3.12"
    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: fake if name == "python3.12" else None
    )
    monkeypatch.setattr(
        doctor, "_python_version_of", lambda python: (3, 12, 4) if python == fake else ()
    )
    value = doctor._resolve_runtime_python()
    assert value.startswith("3.12.4 (")
    assert fake in value and "venv not built yet" in value


def test_doctor_runtime_row_not_needed_in_cloud(doctor, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", "https://api.cognee.ai")
    assert doctor._resolve_runtime_python() == "Not needed (remote server)"


def test_doctor_runtime_row_is_in_the_report(doctor, monkeypatch):
    monkeypatch.setattr(doctor, "_resolve_runtime_python", lambda: "3.12.4 (plugin venv)")
    monkeypatch.setattr(
        doctor,
        "_check_health",
        lambda url, timeout=5.0: {"reachable": False, "latency_ms": None, "raw_body": None},
    )
    report = doctor.collect_report()
    assert report["runtime_python"] == "3.12.4 (plugin venv)"
    line = next(ln for ln in doctor.format_human(report).splitlines() if "Runtime Python:" in ln)
    assert line.endswith("3.12.4 (plugin venv)")
