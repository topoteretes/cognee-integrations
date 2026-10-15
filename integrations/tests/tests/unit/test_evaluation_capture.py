
import pytest


@pytest.fixture
def capture(suite, monkeypatch, tmp_path, temp_home):
    path = suite.scripts_dir / "_evaluation_capture.py"
    if not path.exists():
        pytest.skip("sampled evaluation capture not packaged")
    from utils.isolation import load_suite_module

    module = load_suite_module(
        suite, "_evaluation_capture", home=temp_home, monkeypatch=monkeypatch
    )
    monkeypatch.setenv("COGNEE_EVALUATION_CAPTURE", "true")
    monkeypatch.setattr(
        module,
        "identity",
        lambda: (
            tmp_path / "pool",
            {
                "dataset_id": "00000000-0000-0000-0000-000000000001",
                "base_url": "http://localhost",
                "principal": "test",
            },
        ),
    )
    import _plugin_common

    monkeypatch.setattr(_plugin_common, "get_session_key", lambda: "host-session")
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: None)
    return module


def test_opt_in_redaction_retry_identity_and_bound(capture, monkeypatch):
    capture.remember_context("alice@example.com owns this evidence")
    capture.enqueue("Who is alice@example.com?", "alice@example.com", event_id="turn-1")
    directory, _ = capture.identity()
    first = next(directory.glob("*.json"))
    original = first.read_text()
    assert "alice@example.com" not in original
    capture.enqueue("Who is alice@example.com?", "alice@example.com", event_id="turn-1")
    assert first.read_text() == original
    for i in range(105):
        capture.enqueue("Question", "Answer", event_id=f"another-{i}")
    assert len(list(directory.glob("*.json"))) == 100
    monkeypatch.setenv("COGNEE_EVALUATION_CAPTURE", "false")
    capture.enqueue("private", "private", event_id="off")
    assert len(list(directory.glob("*.json"))) == 100


def test_background_upload_and_optout(capture, monkeypatch):
    import _plugin_common
    import _quality_summary

    monkeypatch.setattr(_quality_summary, "refresh_current", lambda: None)
    calls = []

    def send(path, body, **kwargs):
        calls.append((path, body))
        return {"status": "accepted", "receipt": "receipt"}

    monkeypatch.setattr(_plugin_common, "_json_http_request", send)
    capture.enqueue("question", "answer", event_id="one")
    capture.drain()
    directory, _ = capture.identity()
    assert not list(directory.glob("*.json"))
    assert calls[0][0] == "/api/v1/evaluations/traces"
    capture.enqueue("question", "answer", event_id="two")
    monkeypatch.setenv("COGNEE_EVALUATION_CAPTURE", "false")
    capture.drain()
    assert len(calls) == 1 and not list(directory.glob("*.json"))
