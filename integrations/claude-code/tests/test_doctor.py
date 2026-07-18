"""Tests for doctor.py — plugin health check command.

Run: pytest integrations/claude-code/tests/test_doctor.py
"""

import importlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def _reload_doctor(tmp_home: Path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_home))
    import doctor

    return importlib.reload(doctor)


def test_build_report_has_required_keys(tmp_path, monkeypatch):
    doc = _reload_doctor(tmp_path, monkeypatch)
    r = doc.build_report()
    for key in ("mode", "server", "recall_breaker", "last_recall", "issues", "versions"):
        assert key in r


def test_breaker_closed_when_no_file(tmp_path, monkeypatch):
    doc = _reload_doctor(tmp_path, monkeypatch)
    # avoid real network calls — no server configured in this clean env
    monkeypatch.setattr(doc, "_check_server", lambda url: {"status": "local_sdk"})
    r = doc.build_report()
    assert not r["recall_breaker"]["open"]
    assert r["issues"] == []


def test_breaker_open_when_in_cooldown(tmp_path, monkeypatch):
    doc = _reload_doctor(tmp_path, monkeypatch)
    p = tmp_path / ".cognee-plugin" / "recall-breaker.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"cooldown_until": time.time() + 3600, "failure_count": 3}))
    importlib.reload(doc)
    r = doc.build_report()
    assert r["recall_breaker"]["open"]
    assert r["recall_breaker"]["failures"] == 3
    assert any("recall breaker" in i for i in r["issues"])


def test_last_recall_loaded_from_file(tmp_path, monkeypatch):
    doc = _reload_doctor(tmp_path, monkeypatch)
    recall_path = tmp_path / ".cognee-plugin" / "claude-code" / "last_recall.json"
    recall_path.parent.mkdir(parents=True, exist_ok=True)
    recall_path.write_text(json.dumps({"ts": "2025-01-01T00:00:00+00:00", "hits": {"session": 3}}))
    importlib.reload(doc)
    r = doc.build_report()
    assert r["last_recall"]["ts"] == "2025-01-01T00:00:00+00:00"
    assert r["last_recall"]["hits"] == {"session": 3}


def test_json_flag_emits_valid_json(tmp_path, monkeypatch, capsys):
    doc = _reload_doctor(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["doctor.py", "--json"])
    doc.main()
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "issues" in parsed


def test_exit_code_1_on_server_error(tmp_path, monkeypatch):
    doc = _reload_doctor(tmp_path, monkeypatch)
    monkeypatch.setattr(doc, "_check_server", lambda url: {"status": "error", "error": "refused"})
    monkeypatch.setattr(sys, "argv", ["doctor.py"])
    # Inject a non-empty base_url so the issue is recorded
    original_build = doc.build_report

    def patched_build():
        r = original_build()
        r["issues"].append("server unreachable at http://fake:8011")
        return r

    monkeypatch.setattr(doc, "build_report", patched_build)
    code = doc.main()
    assert code == 1


def test_exit_code_0_when_healthy(tmp_path, monkeypatch, capsys):
    doc = _reload_doctor(tmp_path, monkeypatch)
    monkeypatch.setattr(doc, "_check_server", lambda url: {"status": "local_sdk"})
    monkeypatch.setattr(sys, "argv", ["doctor.py"])
    code = doc.main()
    assert code == 0
