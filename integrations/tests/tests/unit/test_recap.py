"""``cognee-recap.py`` — standup / digest / timeline over the existing server endpoints.

The wrapper behind the ``cognee-standup``, ``cognee-digest`` and
``cognee-timeline`` skills. It adds no server surface: sessions come from
``GET /api/v1/sessions`` (+ ``/{id}``), learnings from a context-only graph
recall. What it owns is the shaping, and that is what is pinned here:

  * the time window: ``--since`` grammar, calendar words at LOCAL midnight,
    the coarsest server ``range`` that still covers the cutoff;
  * session shaping: prompts flattened to one line, edit tools -> files
    edited, the project attributed from the prompt cwd, else the git root of
    the edited files, else the launch record; a prompt-less session keeps no
    server label ("Shell" is a tool name, not a title);
  * graph passages: split on the ``---`` separators, session/date lifted from
    the ``# Session learning — <date> (session <id>)`` header, trailing
    sections ("## Relevant entities") dropped, one line each;
  * the window filter for learnings: header date wins, undated ones ride on
    their session, neither -> dropped;
  * the timeline keeps a calendar-day learning as a day (no UTC-midnight
    shift into the previous local day) and sorts learnings before prompts of
    the same day;
  * the server client: coding-agent ids only unless ``--all-sessions``,
    pagination, activity filter; a 404 detail is an empty session, not a crash;
  * renderers: skeleton sections and the no-data messages;
  * ``main``: an unreachable server is one stderr line and exit 1.

Claude Code only — the other hosts do not ship these skills.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

CLAUDE = "claude-code"


@pytest.fixture
def recap(suite, hook_module, monkeypatch):
    if suite.name != CLAUDE:
        pytest.skip("recap skills ship with the Claude Code plugin only")
    mod = hook_module(suite, "cognee-recap.py")
    # Never touch a real server or launch record from a unit test.
    monkeypatch.setattr(mod, "_local_api_url", lambda: "http://127.0.0.1:1")
    monkeypatch.setattr(
        mod,
        "shell_runtime_overrides",
        lambda *_a, **_k: {
            "host_key": "",
            "session_id": "",
            "dataset": "",
            "dataset_id": "",
            "dataset_ids": "",
            "api_key": "k",
        },
    )
    monkeypatch.setattr(mod, "resolve_active_dataset", lambda *_a, **_k: "agent_sessions")
    monkeypatch.setattr(mod, "launch_cwds", lambda: {})
    return mod


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Time window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, delta, label",
    [
        ("24h", timedelta(hours=24), "last 24h"),
        ("7d", timedelta(days=7), "last 7d"),
        ("2w", timedelta(weeks=2), "last 2w"),
        ("2 d", timedelta(days=2), "last 2d"),
        ("month", timedelta(days=30), "last 30 days"),
    ],
)
def test_parse_since_relative(recap, text, delta, label):
    cutoff, got = recap.parse_since(text, now=NOW)
    assert cutoff == NOW - delta
    assert got == label


def test_parse_since_calendar_words_are_local_midnights(recap):
    midnight = NOW.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    assert recap.parse_since("today", now=NOW) == (midnight, "today")
    assert recap.parse_since("yesterday", now=NOW)[0] == midnight - timedelta(days=1)
    week, label = recap.parse_since("week", now=NOW)
    assert week.weekday() == 0 and week <= midnight and "Monday" in label
    assert recap.parse_since("all", now=NOW)[0].year == 2000


def test_parse_since_iso_date_and_garbage(recap):
    cutoff, label = recap.parse_since("2026-09-20", now=NOW)
    assert cutoff == datetime(2026, 9, 20, tzinfo=timezone.utc)
    assert label == "since 2026-09-20"
    with pytest.raises(SystemExit, match="cannot parse --since"):
        recap.parse_since("fortnight", now=NOW)


@pytest.mark.parametrize(
    "hours, bucket",
    [(1, "24h"), (24, "24h"), (25, "7d"), (24 * 7, "7d"), (24 * 8, "30d"), (24 * 31, "all")],
)
def test_range_bucket_is_the_coarsest_cover(recap, hours, bucket):
    assert recap.range_bucket(NOW - timedelta(hours=hours), now=NOW) == bucket


def test_parse_time_treats_naive_as_utc(recap):
    assert recap.parse_time("2026-09-24T10:00:00").tzinfo is timezone.utc
    assert recap.parse_time("2026-09-24T10:00:00+02:00").utcoffset() == timedelta(hours=2)
    assert recap.parse_time("") is None and recap.parse_time("nope") is None


# ---------------------------------------------------------------------------
# Session shaping
# ---------------------------------------------------------------------------


def _row(
    sid="claude_abc", status="completed", start="2026-09-24T08:00:00", last="2026-09-24T09:30:00"
):
    return {
        "session_id": sid,
        "effective_status": status,
        "started_at": start,
        "last_activity_at": last,
        "cost_usd": 0.5,
    }


def _qa(question, cwd="/home/u/proj", answer="done"):
    return {"question": question, "answer": answer, "context": json.dumps({"cwd": cwd})}


def _trace(tool, status="success", **params):
    return {"origin_function": tool, "status": status, "method_params": params}


def test_summarize_session_shapes_prompts_edits_and_project(recap):
    detail = {
        "label": "fix the flaky test",
        "msg_count": 3,
        "tool_calls": 40,
        "qas": [
            _qa("fix the\n  flaky   test"),
            _qa("now run it"),
            _qa("ship it", answer="pushed"),
        ],
        "traces": [
            _trace("Read", file_path="/home/u/proj/a.py"),
            _trace("Edit", file_path="/home/u/proj/a.py"),
            _trace("Write", file_path="/home/u/proj/tests/test_a.py"),
            _trace("Edit", file_path="/home/u/proj/a.py"),  # duplicate collapses
            _trace("Shell", status="error", command="pytest"),
            _trace("apply_patch", status="success", method_params="not json"),
        ],
    }
    rec = recap.summarize_session(_row(), detail, {})
    assert rec["prompts"] == ["fix the flaky test", "now run it", "ship it"]
    assert rec["label"] == "fix the flaky test"
    assert rec["project"] == "proj" and rec["cwd"] == "/home/u/proj"
    assert rec["files_edited"] == ["a.py", "tests/test_a.py"]
    assert rec["prompt_count"] == 3 and rec["tool_calls"] == 40
    assert rec["errors"] == 1
    assert rec["tools"]["Edit"] == 2
    assert rec["duration"] == "1h 30m"
    assert rec["status"] == "completed"
    assert rec["last_answer"] == "pushed"
    assert rec["agent"] == "claude"


def test_summarize_session_promptless_drops_tool_label_and_uses_edit_git_root(recap, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    target = repo / "src" / "x.py"
    target.write_text("x = 1\n")
    detail = {
        "label": "Shell",  # the server's first-tool label for a prompt-less session
        "msg_count": 0,
        "tool_calls": 12,
        "qas": [],
        "traces": [_trace("Shell", command="ls"), _trace("Edit", file_path=str(target))],
    }
    rec = recap.summarize_session(_row(), detail, {"claude_abc": "/some/plugin/cache/dir"})
    assert rec["label"] == ""
    assert rec["cwd"] == str(repo)
    assert rec["project"] == "repo"
    assert rec["files_edited"] == ["src/x.py"]


def test_summarize_session_falls_back_to_launch_record_cwd(recap):
    detail = {"qas": [], "traces": [_trace("Read", file_path="/nowhere/at/all.py")]}
    rec = recap.summarize_session(_row(sid="codex_1"), detail, {"codex_1": "/home/u/other"})
    assert rec["cwd"] == "/home/u/other" and rec["project"] == "other"
    assert rec["agent"] == "codex"
    assert rec["files_edited"] == []  # Read is not an edit


def test_project_from_edits_ignores_tmp_and_unknown_paths(recap, tmp_path):
    assert recap.project_from_edits(["/tmp/scratch/x.py", "/does/not/exist.py", ""]) == ""
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    f = repo / "f.py"
    f.write_text("")
    assert recap.project_from_edits([str(f), "/tmp/y.py"]) == str(repo)


def test_project_of_reads_cwd_from_qa_context_string_or_dict(recap):
    assert recap.project_of([{"context": json.dumps({"cwd": "/a/b"})}]) == "/a/b"
    assert recap.project_of([{"context": {"cwd": "/c/d"}}]) == "/c/d"
    assert recap.project_of([{"context": "garbage"}], "/fallback") == "/fallback"


@pytest.mark.parametrize(
    "sid, agent",
    [
        ("claude_x", "claude"),
        ("codex_x", "codex"),
        ("antigravity_x", "antigravity"),
        ("agy_x", "antigravity"),
        ("brain-1", "brain"),  # --all-sessions rows: the id's first token names the source
        ("", "?"),
    ],
)
def test_agent_of(recap, sid, agent):
    assert recap.agent_of(sid) == agent


# ---------------------------------------------------------------------------
# Graph passages
# ---------------------------------------------------------------------------

GRAPH_TEXT = (
    "The question is: `x`\nAnswer using this sectioned context.\n\nContext:\n"
    "`## Relevant passages\n"
    "# Session learning — 2026-09-20 (session codex_aaa)\n\n"
    "Prefer the launch record over env.\n(Learned while debugging.)\n"
    "---\n"
    "Session ID: claude_bbb\nSome older-format passage.\n"
    "---\n"
    "Question: what is X?\n\nAnswer: X is Y.\n"
    "\n## Relevant entities\n### session_learning\nshould not appear\n`\n"
)


def test_split_passages_lifts_session_and_date_and_drops_trailing_sections(recap):
    out = recap.split_passages(GRAPH_TEXT)
    assert [p["session_id"] for p in out] == ["codex_aaa", "claude_bbb", ""]
    assert [p["date"] for p in out] == ["2026-09-20", "", ""]
    assert out[0]["text"] == "Prefer the launch record over env. (Learned while debugging.)"
    assert out[1]["text"] == "Some older-format passage."
    assert out[2]["text"] == "Question: what is X? Answer: X is Y."
    assert not any(
        "Relevant entities" in p["text"] or "should not appear" in p["text"] for p in out
    )


def test_split_passages_without_marker_is_a_single_passage(recap):
    out = recap.split_passages("plain text\nwith lines")
    assert out == [{"session_id": "", "date": "", "text": "plain text with lines"}]
    assert recap.split_passages("") == []


def test_in_window_date_wins_then_session_then_drop(recap):
    cutoff = datetime(2026, 9, 17, tzinfo=timezone.utc)
    passages = [
        {"session_id": "s_in", "date": "2026-09-01", "text": "old learning of an active session"},
        {"session_id": "s_out", "date": "2026-09-20", "text": "fresh learning of an old session"},
        {"session_id": "s_in", "date": "", "text": "undated, session in window"},
        {"session_id": "", "date": "", "text": "nothing places this in time"},
    ]
    kept = recap.in_window(passages, {"s_in"}, cutoff)
    assert [p["text"] for p in kept] == [
        "fresh learning of an old session",
        "undated, session in window",
    ]


# ---------------------------------------------------------------------------
# Server client
# ---------------------------------------------------------------------------


class _FakeHTTP:
    """Stand-in for ``_json_http_request``: canned GET bodies + a request log."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(
        self, path, payload=None, *, method="POST", timeout=30.0, base_url=None, api_key=None
    ):
        self.calls.append(path)
        for prefix, body in self.routes.items():
            if path.startswith(prefix):
                if isinstance(body, Exception):
                    raise body
                return body(path) if callable(body) else body
        raise urllib.error.HTTPError(path, 404, "nf", {}, None)


def test_sessions_paginates_filters_agents_and_activity(recap, monkeypatch):
    now = datetime.now(timezone.utc)  # range_bucket measures the cutoff against the real clock
    cutoff = now - timedelta(hours=12)
    stamp = lambda h: (now - timedelta(hours=h)).replace(tzinfo=None).isoformat()  # noqa: E731
    page1 = {
        "sessions": [
            _row("claude_new", last=stamp(2)),
            _row("brain-7", last=stamp(2)),  # not a coding agent
            _row("codex_old", last=stamp(48)),  # before cutoff
        ],
        "has_more": True,
    }
    page2 = {"sessions": [_row("agy_recent", last=stamp(1))], "has_more": False}

    def route(path):
        return page2 if "offset=200" in path else page1

    fake = _FakeHTTP({"/api/v1/sessions?": route})
    monkeypatch.setattr(recap, "_json_http_request", fake)
    server = recap.Server()
    assert server.dataset == "agent_sessions"
    rows = server.sessions(cutoff, all_sessions=False)
    assert [r["session_id"] for r in rows] == ["agy_recent", "claude_new"]  # newest first
    assert len(fake.calls) == 2 and "range=24h" in fake.calls[0]
    rows_all = server.sessions(cutoff, all_sessions=True)
    assert "brain-7" in {r["session_id"] for r in rows_all}


def test_detail_404_is_empty_and_other_errors_raise(recap, monkeypatch):
    fake = _FakeHTTP(
        {
            "/api/v1/sessions/gone": urllib.error.HTTPError("x", 404, "nf", {}, None),
            "/api/v1/sessions/broken": urllib.error.HTTPError("x", 500, "boom", {}, None),
            "/api/v1/sessions/ok": {"session_id": "ok", "qas": []},
        }
    )
    monkeypatch.setattr(recap, "_json_http_request", fake)
    server = recap.Server()
    assert server.detail("gone") == {}
    assert server.detail("ok")["session_id"] == "ok"
    with pytest.raises(urllib.error.HTTPError):
        server.detail("broken")
    assert any("sessions/ok" in c for c in fake.calls)


def test_passages_uses_graph_scope_context_only_and_splits(recap, monkeypatch):
    seen = {}

    def fake_recall(query, **kw):
        seen.update(kw, query=query)
        return [{"kind": "graph_completion", "text": GRAPH_TEXT}]

    monkeypatch.setattr(recap, "recall_via_http", fake_recall)
    out = recap.Server().passages("launch record", 5)
    assert seen["scope"] == ["graph"] and seen["only_context"] is True
    assert seen["dataset"] == "agent_sessions" and seen["top_k"] == 5
    assert len(out) == 3 and out[0]["session_id"] == "codex_aaa"


# ---------------------------------------------------------------------------
# Collect + render
# ---------------------------------------------------------------------------


def _args(**over):
    base = {"all_sessions": False, "max_sessions": 25, "projects": [], "since": None, "json": False}
    base.update(over)
    return type("Args", (), base)()


def test_collect_sessions_caps_and_filters_projects_and_survives_detail_errors(recap, monkeypatch):
    rows = [_row("claude_a"), _row("claude_b"), _row("claude_c")]

    class FakeServer:
        def sessions(self, cutoff, *, all_sessions):
            return rows

        def detail(self, sid):
            if sid == "claude_b":
                raise RuntimeError("kaput")
            cwd = "/w/alpha" if sid == "claude_a" else "/w/beta"
            return {"qas": [_qa(f"work in {sid}", cwd=cwd)], "traces": []}

    recs = recap.collect_sessions(FakeServer(), NOW - timedelta(days=1), _args(max_sessions=2))
    assert [r["session_id"] for r in recs] == ["claude_a", "claude_b"]
    assert recs[1]["prompts"] == []  # the broken detail became an empty session
    only = recap.collect_sessions(FakeServer(), NOW - timedelta(days=1), _args(projects=["alpha"]))
    assert [r["session_id"] for r in only] == ["claude_a"]


def test_collect_timeline_merges_learnings_and_prompts_in_date_order(recap):
    sessions = [
        recap.summarize_session(
            _row("claude_s", start="2026-09-22T08:00:00", last="2026-09-22T10:38:00"),
            {"qas": [_qa("what about the Observer proxy?"), _qa("unrelated")], "traces": []},
            {},
        )
    ]

    class FakeServer:
        def passages(self, topic, top_k):
            return [
                {"session_id": "codex_x", "date": "2026-09-22", "text": "observer: use safe mode"},
                {
                    "session_id": "claude_s",
                    "date": "",
                    "text": "undated learning, dated by session",
                },
                {"session_id": "", "date": "2026-08-01", "text": "too old"},
            ]

    events = recap.collect_timeline(
        FakeServer(), "observer", datetime(2026, 9, 1, tzinfo=timezone.utc), sessions
    )
    assert [(e["kind"], e["text"]) for e in events] == [
        ("learning", "observer: use safe mode"),
        ("learning", "undated learning, dated by session"),
        ("prompt", "what about the Observer proxy?"),
    ]
    assert events[0]["time"] == "2026-09-22"  # a calendar day stays a calendar day
    assert events[2]["project"] == "proj" and events[2]["agent"] == "claude"


def test_event_day_keeps_calendar_days_and_localises_instants(recap):
    assert recap._event_day("2026-09-05") == ("2026-09-05 Sat", "  —  ")
    day, clock = recap._event_day("2026-09-22T10:38:00+00:00")
    assert day.startswith("2026-09-2") and ":" in clock
    assert recap._event_day(None) == ("(undated)", "--:--")


def test_render_standup_groups_by_project_and_lists_open_work(recap):
    recs = [
        recap.summarize_session(
            _row("claude_a", status="running"),
            {
                "qas": [
                    _qa("start the migration", cwd="/w/alpha"),
                    _qa("why does it hang?", cwd="/w/alpha"),
                ],
                "traces": [],
            },
            {},
        ),
        recap.summarize_session(
            _row("codex_b"),
            {"qas": [], "traces": [_trace("Bash", command="ls"), _trace("Bash", command="pwd")]},
            {"codex_b": "/w/beta"},
        ),
    ]
    text = recap.render_standup(recs, "last 24h", NOW - timedelta(days=1))
    assert text.startswith("# Standup — last 24h")
    assert "## alpha" in text and "## beta" in text
    assert '"start the migration"' in text
    assert "no prompts captured" in text and "Bash ×2" in text
    assert "## Left open" in text and "alpha: why does it hang?" in text
    assert "Shell" not in text
    assert recap.render_standup([], "today", NOW).endswith(
        "_No coding-agent sessions in this window._"
    )


def test_render_digest_has_totals_days_files_and_learnings(recap):
    recs = [
        recap.summarize_session(
            _row("claude_a"),
            {
                "msg_count": 2,
                "tool_calls": 7,
                "qas": [_qa("add the digest", cwd="/w/alpha")],
                "traces": [_trace("Edit", file_path="/w/alpha/recap.py")],
            },
            {},
        )
    ]
    passages = [
        {"session_id": "claude_a", "date": "2026-09-23", "text": "Digest learnings are dated."}
    ]
    text = recap.render_digest(recs, passages, "last 7d", NOW - timedelta(days=7))
    assert (
        "**1 sessions · 2 prompts · 7 tool calls · 1 distinct files edited** across alpha" in text
    )
    assert "### alpha" in text and "recap.py (1 session)" in text
    assert "## Learnings recorded in the graph\n- 2026-09-23 · Digest learnings are dated." in text
    assert recap.render_digest([], [], "x", NOW).endswith("_Nothing recorded in this window._")


def test_render_timeline_days_tags_and_empty(recap):
    events = [
        {
            "time": "2026-09-05",
            "kind": "learning",
            "session_id": "codex_x",
            "project": "alpha",
            "agent": "codex",
            "text": "L1",
        },
        {
            "time": "2026-09-22T10:38:00+00:00",
            "kind": "prompt",
            "session_id": "claude_s",
            "project": "",
            "agent": "claude",
            "text": "P1",
        },
    ]
    text = recap.render_timeline("observer", events, "last 30d")
    assert text.startswith("# Timeline — 'observer' · last 30d · 2 events")
    assert "## 2026-09-05 Sat\n-   —   [learned · alpha · codex] L1" in text
    assert "[asked · claude] P1" in text
    assert "Nothing about this topic" in recap.render_timeline("x", [], "last 30d")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_unreachable_server_is_one_stderr_line_and_exit_1(recap, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(recap, "_json_http_request", boom)
    assert recap.main(["standup"]) == 1
    err = capsys.readouterr().err
    assert "unreachable" in err and "cognee-doctor.sh" in err


def test_main_json_payloads_per_mode(recap, monkeypatch, capsys):
    monkeypatch.setattr(
        recap,
        "_json_http_request",
        _FakeHTTP(
            {
                "/api/v1/sessions?": {
                    "sessions": [_row("claude_a", last=(NOW - timedelta(hours=1)).isoformat())],
                    "has_more": False,
                },
                "/api/v1/sessions/claude_a": {
                    "qas": [_qa("digest work", cwd="/w/alpha")],
                    "traces": [],
                },
            }
        ),
    )
    monkeypatch.setattr(
        recap,
        "recall_via_http",
        lambda *a, **k: [
            {
                "text": "## Relevant passages\n# Session learning — 2100-01-01 "
                "(session claude_a)\n\nfuture-proof\n`\n"
            }
        ],
    )
    assert recap.main(["standup", "--json"]) == 0
    standup = json.loads(capsys.readouterr().out)
    assert standup["mode"] == "standup" and standup["sessions"][0]["project"] == "alpha"
    assert "learnings" not in standup

    assert recap.main(["digest", "--json"]) == 0
    digest = json.loads(capsys.readouterr().out)
    assert digest["learnings"][0]["text"] == "future-proof"

    assert recap.main(["timeline", "digest", "--json"]) == 0
    timeline = json.loads(capsys.readouterr().out)
    assert timeline["topic"] == "digest"
    assert sorted(e["kind"] for e in timeline["events"]) == ["learning", "prompt"]


def test_main_digest_reports_learnings_outside_the_window(recap, monkeypatch, capsys):
    monkeypatch.setattr(
        recap,
        "_json_http_request",
        _FakeHTTP({"/api/v1/sessions?": {"sessions": [], "has_more": False}}),
    )
    monkeypatch.setattr(
        recap,
        "recall_via_http",
        lambda *a, **k: [
            {
                "text": "## Relevant passages\n# Session learning — 2020-01-01 "
                "(session codex_z)\n\nancient\n`\n"
            }
        ],
    )
    assert recap.main(["digest"]) == 0
    captured = capsys.readouterr()
    assert "none are dated in the window" in captured.err
    assert "ancient" not in captured.out
