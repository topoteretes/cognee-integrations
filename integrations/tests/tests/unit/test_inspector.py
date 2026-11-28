"""The inspector (claude-code/tools/inspector): a read-only timeline for development.

It only reads: the plugin's hook log, the server's log and the server's run listing.
These tests feed it files and a fake server and check what reaches the page.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

INSPECTOR = (
    Path(__file__).resolve().parents[3] / "claude-code" / "tools" / "inspector" / "inspector.py"
)
SESSION = "5c492456-a515-4807-8cad-9a0d4c79a2f7"


@pytest.fixture
def inspector(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("INSPECTOR_SERVER_URL", "http://127.0.0.1:9")
    spec = importlib.util.spec_from_file_location("cognee_inspector_under_test", INSPECTOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.HOOK_LOG = tmp_path / "hook.log"
    module.SERVER_LOG = tmp_path / "server.log"
    module.KEY_FILE = tmp_path / "api_key.json"
    return module


def _hook(event: str, detail, pid: int = 1, ts: str = "2026-11-28T13:12:35+00:00") -> str:
    return json.dumps({"ts": ts, "pid": pid, "event": event, "detail": detail})


def test_hook_events_carry_their_session_and_hide_secrets(inspector):
    inspector.HOOK_LOG.write_text(
        "\n".join(
            [
                _hook("store_session_key", {"value": SESSION}, pid=7),
                # Same hook process, no session in the line: attributed by pid.
                _hook("coding_unit_captured", {"traces": 5, "qa": 1}, pid=7),
                _hook("agent_register_failed", {"status": 403, "api_key": "sk-secret"}, pid=8),
                _hook("runtime_state_connection_lookup_failed", {"error": "timed out"}, pid=9),
                "not json at all",
            ]
        )
        + "\n"
    )
    rows = {row["title"]: row for row in inspector.hook_events()}
    assert rows["coding_unit_captured"]["session"] == SESSION
    assert rows["agent_register_failed"]["level"] == "error"
    assert rows["agent_register_failed"]["detail"]["api_key"] == "<redacted>"
    assert rows["agent_register_failed"]["session"] == ""
    # A state lookup that misses is frequent and harmless: not an error row.
    assert rows["runtime_state_connection_lookup_failed"]["level"] == "info"
    assert "sk-secret" not in json.dumps(list(rows.values()))


def test_server_log_keeps_the_lines_worth_a_row(inspector):
    lines = [
        # The server colours its log: the codes are stripped before matching.
        "\x1b[2m2026-11-28T13:17:00.1\x1b[0m [\x1b[32minfo     \x1b[0m] "
        "Running enola: /x/enola check -format=json",
        "2026-11-28T13:17:01.2 [info     ] "
        f"improve: distilled session 'claude_{SESSION}' -> documents=1",
        "2026-11-28T13:17:02.3 [error    ] PermissionDeniedError raised (Status code: 403)",
        "2026-11-28T13:17:03.4 [info     ] Neighborhood retrieval (1-hop): 5 nodes",
    ]
    inspector.SERVER_LOG.write_text("\n".join(lines) + "\n")
    rows = inspector.server_log()
    assert [row["source"] for row in rows] == ["enola", "server", "server"]
    assert rows[1]["session"] == SESSION
    assert rows[2]["level"] == "error"


def test_runs_show_their_stages_and_an_unreachable_server_is_reported(inspector, monkeypatch):
    rows, error = inspector.server_runs()
    assert rows == [] and error  # no key file, nothing listening: said, not raised

    inspector.KEY_FILE.write_text(json.dumps({"api_key": "k"}))
    runs = [
        {
            "operation_name": "improve",
            "outcome": "succeeded",
            "dataset_name": "agent_sessions",
            "started_at": "2026-11-28T13:17:00.000000",
            "session_ids": [f"claude_{SESSION}"],
            "stages": [
                {"stage": "distill_sessions", "status": "completed", "counts": {"documents": 1}}
            ],
        },
        {"pipeline_name": "cognify_pipeline", "status": "DATASET_PROCESSING_ERRORED"},
    ]

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(runs).encode()

    seen = []

    def fake_urlopen(request, timeout=None):
        seen.append((request.full_url, request.get_header("X-api-key")))
        return Reply()

    monkeypatch.setattr(inspector.urllib.request, "urlopen", fake_urlopen)
    rows, error = inspector.server_runs()
    assert error == ""
    assert seen == [("http://127.0.0.1:9/api/v1/activity/pipeline-runs?limit=60", "k")]
    assert rows[0]["title"] == "improve: succeeded (agent_sessions)"
    assert rows[0]["session"] == SESSION
    assert rows[0]["stages"][0]["stage"] == "distill_sessions"
    assert rows[1]["level"] == "error" and rows[1]["stages"] is None
