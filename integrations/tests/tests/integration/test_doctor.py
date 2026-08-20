"""`cognee-doctor`'s health probe and full report, against a real server.

`_check_health` issues a genuine `GET {base}/health`, so the mock serves it here
and an absent server is a genuinely closed port rather than a hand-thrown
URLError. That also fixes a real hazard in the migrated tests: their
`collect_report()` case ran with **no** urlopen patch at all, so it made a live
network call to whatever URL the developer's environment happened to resolve.

Migrated from claude-code/tests/test_doctor.py and
codex/plugins/cognee/tests/test_doctor.py (whose copy stays in place for the
Windows workflow); the purely local resolution logic is in
unit/test_doctor_resolution.py.
"""

from __future__ import annotations

import json

import pytest

HEALTH = "/health"

_REPORT_KEYS = {
    "mode",
    "env_file",
    "server_url",
    "api_key_source",
    "reachable",
    "latency_ms",
    "cognee_local",
    "cognee_server",
    "embedding_model",
    "embedding_dimensions",
    "circuit_breaker",
    "dataset",
    "dataset_source",
}


@pytest.fixture
def doctor(suite, isolated_modules, mock_server, monkeypatch):
    module = isolated_modules(suite, "doctor")
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    return module


def test_health_reachable(doctor, mock_server):
    result = doctor._check_health(mock_server.url)
    assert result["reachable"] is True
    assert result["latency_ms"] is not None and result["latency_ms"] >= 0
    mock_server.assert_called("GET", HEALTH)


def test_health_unreachable_on_a_closed_port(doctor, closed_port_url):
    result = doctor._check_health(closed_port_url)
    assert result["reachable"] is False
    assert result["latency_ms"] is None


def test_health_unreachable_when_the_server_errors(doctor, mock_server):
    mock_server.set_health_status(503)
    assert doctor._check_health(mock_server.url)["reachable"] is False


def test_json_report_has_exactly_the_expected_keys(doctor, mock_server):
    report = doctor.collect_report()
    parsed = json.loads(doctor.format_json(report))
    assert set(parsed.keys()) == _REPORT_KEYS, f"keys mismatch: {_REPORT_KEYS ^ set(parsed.keys())}"


def test_report_sees_the_running_server(doctor, mock_server):
    """The probe result is what the report line is built from."""
    report = doctor.collect_report()
    assert report["reachable"] is True
    assert report["mode"] == "Local Managed"  # the mock listens on loopback
    mock_server.assert_called("GET", HEALTH)


def test_report_marks_an_absent_server_unreachable(doctor, monkeypatch, closed_port_url):
    monkeypatch.setenv("COGNEE_BASE_URL", closed_port_url)
    report = doctor.collect_report()
    assert report["reachable"] is False


def test_report_includes_effective_project_dataset(
    doctor, project_dir, monkeypatch
):
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("COGNEE_DATASET_SCOPE", "project")
    report = doctor.collect_report()
    assert report["dataset"].startswith("project_project_")
    assert report["dataset_source"] == "project"
    assert str(project_dir) not in report["dataset"]


def test_human_output_contains_header(doctor, mock_server):
    text = doctor.format_human(doctor.collect_report())
    assert "Cognee Doctor" in text
    assert "Mode:" in text
    assert "Dataset:" in text
    assert "Dataset Source:" in text
    assert "Cognee (local):" in text
    assert "Circuit Breaker:" in text
