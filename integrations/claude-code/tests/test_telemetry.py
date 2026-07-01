"""Tests for telemetry.py — opt-in session-end emitter.

Run: pytest integrations/claude-code/tests/test_telemetry.py
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import telemetry


class _CaptureSink:
    def __init__(self):
        self.calls = []

    def emit(self, event: dict) -> None:
        self.calls.append(event)


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("COGNEE_TELEMETRY_ENABLED", raising=False)
    sink = _CaptureSink()
    telemetry.emit_session_end("abc123", sink=sink)
    assert sink.calls == []


def test_enabled_emits_exactly_one_event(monkeypatch):
    monkeypatch.setenv("COGNEE_TELEMETRY_ENABLED", "true")
    sink = _CaptureSink()
    telemetry.emit_session_end("sess-1", mode="local_sdk", sink=sink)
    assert len(sink.calls) == 1


def test_event_has_required_fields(monkeypatch):
    monkeypatch.setenv("COGNEE_TELEMETRY_ENABLED", "true")
    sink = _CaptureSink()
    telemetry.emit_session_end("sess-2", mode="http", sink=sink)
    e = sink.calls[0]
    for key in ("event", "ts", "session_id", "mode", "turns", "saves", "versions"):
        assert key in e, f"missing key: {key}"
    assert e["event"] == "session_end"
    assert e["mode"] == "http"
    assert e["session_id"] == "sess-2"


def test_no_sensitive_fields_in_event(monkeypatch):
    monkeypatch.setenv("COGNEE_TELEMETRY_ENABLED", "true")
    sink = _CaptureSink()
    telemetry.emit_session_end("sess-3", mode="local_sdk", sink=sink)
    serialized = json.dumps(sink.calls[0])
    # "prompt" and "trace" are legitimate save-kind labels in the saves dict;
    # what must never appear is raw prompt/recall text or auth credentials
    for forbidden in ("api_key", "base_url", "password", "token"):
        assert forbidden not in serialized, f"sensitive field present: {forbidden}"


def test_empty_session_id_is_noop(monkeypatch):
    monkeypatch.setenv("COGNEE_TELEMETRY_ENABLED", "true")
    sink = _CaptureSink()
    telemetry.emit_session_end("", sink=sink)
    assert sink.calls == []


def test_local_file_sink_writes_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_TELEMETRY_ENABLED", "true")
    out = tmp_path / "telemetry.jsonl"
    sink = telemetry.LocalFileSink(path=out)
    telemetry.emit_session_end("file-sess", mode="local_sdk", sink=sink)
    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["event"] == "session_end"
    assert parsed["session_id"] == "file-sess"


def test_local_file_sink_appends_multiple_events(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_TELEMETRY_ENABLED", "true")
    out = tmp_path / "telemetry.jsonl"
    sink = telemetry.LocalFileSink(path=out)
    telemetry.emit_session_end("s1", sink=sink)
    telemetry.emit_session_end("s2", sink=sink)
    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2


def test_turn_count_from_counter_file(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_TELEMETRY_ENABLED", "true")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    importlib.reload(telemetry)

    counter = tmp_path / ".cognee-plugin" / "claude-code" / "counter.json"
    counter.parent.mkdir(parents=True, exist_ok=True)
    counter.write_text(json.dumps({"my-sess": 17}))

    sink = _CaptureSink()
    telemetry.emit_session_end("my-sess", sink=sink)
    assert sink.calls[0]["turns"] == 17


def test_save_counts_from_save_counter(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_TELEMETRY_ENABLED", "true")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    importlib.reload(telemetry)

    sc = tmp_path / ".cognee-plugin" / "claude-code" / "save_counter.json"
    sc.parent.mkdir(parents=True, exist_ok=True)
    sc.write_text(json.dumps({"s": {"prompt": 5, "trace": 3, "answer": 2}}))

    sink = _CaptureSink()
    telemetry.emit_session_end("s", sink=sink)
    saves = sink.calls[0]["saves"]
    assert saves == {"prompt": 5, "trace": 3, "answer": 2}


def test_sink_protocol_satisfied_by_local_file_sink():
    assert isinstance(telemetry.LocalFileSink(), telemetry.TelemetrySink)
