"""``cognee-plugin trace``: one session's hook events and server runs in time order.

The hook lines are read from ``hook.log`` as the hooks write them; the server
rows come from ``GET /api/v1/activity/pipeline-runs`` (a fake opener here),
with the ``stages`` list a cognee server on ``feature/improve-stage-results``
puts on improve operation rows. Claude Code only.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from utils.code_analysis import FakeServer

RUNS = "/api/v1/activity/pipeline-runs"
HOST, SESSION = "host-1", "claude_host-1"


def line(ts: str, pid: int, event: str, **detail) -> str:
    entry = {"ts": ts, "pid": pid, "event": event, "event_name": "x." + event, "event_schema": 2}
    if detail:
        entry["detail"] = detail
    return json.dumps(entry)


LOG = [
    line("2026-11-26T10:00:00+00:00", 11, "context_lookup_session_key", value=HOST),
    line("2026-11-26T10:00:01+00:00", 11, "context_lookup_hit", counts={"graph": 1}),
    # Another session's hook process, between this session's.
    line("2026-11-26T10:00:02+00:00", 22, "context_lookup_session_key", value="host-2"),
    line("2026-11-26T10:00:03+00:00", 22, "context_lookup_hit", counts={"graph": 9}),
    line("2026-11-26T10:00:30+00:00", 33, "store_session_key", value=HOST),
    line("2026-11-26T10:00:31+00:00", 33, "stop_stored"),
    "not json",
    # The same pid an hour later is another process.
    line("2026-11-26T11:30:00+00:00", 11, "context_lookup_session_key", value="host-2"),
    line("2026-11-26T11:30:01+00:00", 11, "context_lookup_hit", counts={"graph": 5}),
    # Named by its Cognee session id only.
    line("2026-11-26T10:01:00+00:00", 44, "bootstrap_complete", session_id=SESSION),
]
IMPROVE = {
    "id": "r1",
    "kind": "operation",
    "operation_name": "improve",
    "outcome": "failed",
    "background": True,
    "error_class": "LLMPaymentRequiredError",
    "dataset_name": "agent_sessions",
    "created_at": "2026-11-26T10:00:40",
    "stages": [
        {
            "stage": "feedback_weights",
            "status": "skipped",
            "reason": "opt_in_disabled",
            "error": None,
            "counts": {},
            "duration_ms": 0,
        },
        {
            "stage": "persist_session_qa",
            "status": "completed",
            "reason": None,
            "error": None,
            "counts": {"qa": 2},
            "duration_ms": 812,
        },
        {
            "stage": "distill_sessions",
            "status": "errored",
            "reason": None,
            "error": "budget exhausted",
            "counts": {},
            "duration_ms": 95,
        },
    ],
}
PIPELINE = {
    "id": "r2",
    "kind": "pipeline",
    "pipeline_name": "cognify_pipeline",
    "operation_name": "cognify_pipeline",
    "status": "DATASET_PROCESSING_COMPLETED",
    "outcome": None,
    "created_at": "2026-11-26T10:00:20.500000+00:00",
}


@pytest.fixture
def env(suite, isolated_modules, monkeypatch, temp_home):
    if suite.name != "claude-code":
        pytest.skip("trace ships with the Claude Code plugin")
    plugin = isolated_modules(suite, "cognee_plugin")
    import _code_analysis
    import _trace

    server = FakeServer()
    monkeypatch.setattr(_code_analysis, "_HTTPS_OPENER", SimpleNamespace(open=server))
    monkeypatch.setattr(
        _code_analysis, "runtime", lambda: {**server.rt, "host_key": "", "dataset_id": ""}
    )
    state = plugin._PLUGIN_DIR
    (state / "sessions").mkdir(parents=True)
    (state / "sessions" / (HOST + ".json")).write_text(
        json.dumps({"host_key": HOST, "session_id": SESSION, "dataset": "agent_sessions"})
    )
    (state / "hook.log").write_text("\n".join(LOG) + "\n")
    return SimpleNamespace(
        plugin=plugin, trace=_trace, server=server, state=state, ca=_code_analysis
    )


def run(env, *argv) -> list:
    out: list = []
    assert env.trace.main(env.state, *argv, out=out.append) == 0
    return out


def test_hook_events_and_server_runs_interleave_in_time_order(env):
    env.server.on("GET", RUNS, (200, [IMPROVE, PIPELINE]))
    out = run(env, HOST, False)
    assert out[0] == (
        f"# session {HOST} (Cognee session {SESSION}, dataset agent_sessions), "
        f"server {env.server.url}"
    )
    assert out[1:] == [
        '2026-11-26T10:00:00Z hook   context_lookup_session_key {"value": "host-1"}',
        '2026-11-26T10:00:01Z hook   context_lookup_hit {"counts": {"graph": 1}}',
        "2026-11-26T10:00:20Z server pipeline cognify_pipeline: DATASET_PROCESSING_COMPLETED",
        '2026-11-26T10:00:30Z hook   store_session_key {"value": "host-1"}',
        "2026-11-26T10:00:31Z hook   stop_stored",
        "2026-11-26T10:00:40Z server operation improve: failed (background) "
        "error=LLMPaymentRequiredError dataset=agent_sessions",
        "2026-11-26T10:00:40Z server   improve stage feedback_weights: skipped "
        "(opt_in_disabled) 0ms",
        "2026-11-26T10:00:40Z server   improve stage persist_session_qa: completed 812ms",
        "2026-11-26T10:00:40Z server   improve stage distill_sessions: errored "
        "error=budget exhausted 95ms",
        '2026-11-26T10:01:00Z hook   bootstrap_complete {"session_id": "claude_host-1"}',
    ]
    assert all("\n" not in text for text in out)


def test_the_server_is_asked_for_the_sessions_rows(env):
    env.server.on("GET", RUNS, (200, []))
    run(env, HOST, False)
    (call,) = [c for c in env.server.calls if c[1] == RUNS]
    assert call[0] == "GET"


def test_an_improve_row_without_stages_is_one_line(env):
    env.server.on("GET", RUNS, (200, [{**IMPROVE, "stages": None}]))
    assert [t for t in run(env, HOST, False) if "improve" in t] == [
        "2026-11-26T10:00:40Z server operation improve: failed (background) "
        "error=LLMPaymentRequiredError dataset=agent_sessions"
    ]


def test_the_session_can_be_named_by_its_cognee_session_id_or_defaults_to_the_newest(env):
    env.server.on("GET", RUNS, (200, []))
    by_cognee_id = run(env, SESSION, False)
    assert by_cognee_id == run(env, "", False)
    assert by_cognee_id[0].startswith(f"# session {HOST} ")


def test_a_server_that_cannot_be_read_costs_one_line(env):
    env.server.on("GET", RUNS, ConnectionRefusedError("refused"))
    out = run(env, HOST, False)
    assert sum(text.startswith("# server runs not read") for text in out) == 1
    assert sum(" hook " in text for text in out) == 5


def test_no_session_at_all(env):
    (env.state / "sessions" / (HOST + ".json")).unlink()
    out: list = []
    assert env.trace.main(env.state, "", False, out=out.append) == 1
    assert "no session found" in out[0]


def test_follow_prints_only_what_is_new_and_stops_on_interrupt(env):
    env.server.on("GET", RUNS, (200, [PIPELINE]))
    out: list = []
    polls = []

    def sleep(seconds):
        polls.append(seconds)
        if len(polls) == 1:
            with (env.state / "hook.log").open("a") as handle:
                handle.write(
                    line("2026-11-26T10:02:00+00:00", 55, "stop_stored", session=HOST) + "\n"
                )
            env.server.on("GET", RUNS, (200, [PIPELINE, IMPROVE]))
            return
        if len(polls) == 2:
            return
        raise KeyboardInterrupt

    assert env.trace.main(env.state, HOST, True, out=out.append, sleep=sleep) == 0
    assert len(out) == len(set(out))
    assert out[-1] == '2026-11-26T10:02:00Z hook   stop_stored {"session": "host-1"}'
    assert sum("improve stage" in text for text in out) == 3
    before = {p.name for p in env.state.iterdir()}
    assert before == {"sessions", "hook.log"}  # nothing new is written


def test_the_cli_has_the_trace_command(env, capsys):
    env.server.on("GET", RUNS, (200, [IMPROVE]))
    assert env.plugin.main(["trace", "--session", HOST]) == 0
    assert "improve stage persist_session_qa: completed 812ms" in capsys.readouterr().out
    args = env.plugin._build_parser().parse_args(["trace", "--follow"])
    assert args.follow is True and args.session == ""
