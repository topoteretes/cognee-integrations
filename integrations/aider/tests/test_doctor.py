import json

from cognee_integration_aider.cli import main
from cognee_integration_aider.config import AiderCogneeConfig
from cognee_integration_aider.doctor import DoctorCheck, DoctorReport, render_report, run_doctor


def test_run_doctor_reports_invalid_config(monkeypatch, tmp_path):
    config_path = tmp_path / "bad.json"
    config_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("AIDER_COGNEE_CONFIG", str(config_path))
    monkeypatch.setattr(
        "cognee_integration_aider.doctor._check_versions",
        lambda: [DoctorCheck("Cognee package", "ok", "Version 1.0.0 installed")],
    )
    monkeypatch.setattr(
        "cognee_integration_aider.doctor._check_docker",
        lambda timeout: DoctorCheck("Docker", "warn", "Skipped in test"),
    )
    monkeypatch.setattr("cognee_integration_aider.doctor._check_database", lambda timeout: [])

    report = run_doctor(AiderCogneeConfig(), skip_network=True, cwd=tmp_path)

    config_check = next(check for check in report.checks if check.name == "Config file")
    assert config_check.status == "error"
    assert "not valid JSON" in config_check.detail
    assert report.exit_code() == 1


def test_render_report_marks_strict_warnings_as_unhealthy():
    report = DoctorReport([DoctorCheck("Docker", "warn", "Docker is not running", "Start Docker.")])

    assert report.exit_code(strict=False) == 0
    assert report.exit_code(strict=True) == 1
    assert "warnings and --strict was requested" in render_report(report, strict=True)


def test_cli_doctor_json(monkeypatch, capsys, tmp_path):
    config_path = tmp_path / "cognee.json"
    config_path.write_text(json.dumps({"dataset": "repo"}), encoding="utf-8")
    monkeypatch.setenv("AIDER_COGNEE_CONFIG", str(config_path))
    monkeypatch.setattr(
        "cognee_integration_aider.cli.run_doctor",
        lambda config, timeout, skip_network: DoctorReport(
            [DoctorCheck("Python", "ok", "Python 3.12 detected")]
        ),
    )

    exit_code = main(["doctor", "--json", "--skip-network"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ok"
    assert output["checks"][0]["name"] == "Python"


def test_cli_doctor_strict_returns_non_zero_for_warning(monkeypatch, capsys):
    monkeypatch.setattr(
        "cognee_integration_aider.cli.run_doctor",
        lambda config, timeout, skip_network: DoctorReport(
            [DoctorCheck("Docker", "warn", "Docker is not running")]
        ),
    )

    exit_code = main(["doctor", "--strict", "--skip-network"])

    assert exit_code == 1
    assert "[WARN] Docker" in capsys.readouterr().out
