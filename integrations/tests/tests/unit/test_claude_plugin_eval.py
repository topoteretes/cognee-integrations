"""Tests for scripts/plugin_eval.py.

Everything runs against synthetic plugin state in a temp dir: no network, no real
logs. Fixtures use the schemas the plugin actually writes (hook.log lines are
{ts, pid, event, detail}; recall-audit.log rows carry hits / per_scope / context;
Claude Code transcripts carry message.usage and hook attachments).

Location-agnostic on purpose: the scripts dir is found by walking up, so this
file works both next to the plugin and under integrations/tests/tests/unit/.
"""

from __future__ import annotations

import contextlib
import email.message
import io
import json
import pathlib
import sys
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest


def _scripts_dir() -> str:
    for parent in pathlib.Path(__file__).resolve().parents:
        candidate = parent / "integrations" / "claude-code" / "skills" / "cognee-report" / "scripts"
        if (candidate / "plugin_eval.py").is_file():
            return str(candidate)
        sibling = parent / "scripts"
        if (sibling / "plugin_eval.py").is_file():
            return str(sibling)
    raise RuntimeError("plugin_eval.py not found above %s" % __file__)


sys.path.insert(0, _scripts_dir())
import plugin_eval as pe  # noqa: E402

UTC = timezone.utc
SESSION_UUID = "11111111-2222-3333-4444-555555555555"
SESSION_ID = "claude_" + SESSION_UUID
SECRET = "zebra-quartz-secret-prompt-text-that-must-never-leave"
ITEM_A = (
    "[trace] Read - success: the deployment checklist says rotate the staging database "
    "credentials every quarter"
)
ITEM_B = (
    "[trace] Bash - success: pytest reported forty two passed and three skipped in the "
    "payments integration suite"
)


def ts(day: int, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def hook(moment: datetime, event: str, pid: int = 1, **detail) -> dict:
    return {
        "ts": iso(moment),
        "pid": pid,
        "event": event,
        "event_name": event,
        "event_schema": 2,
        "detail": detail,
    }


def recall(
    moment: datetime,
    context: str,
    hits=None,
    per_scope=None,
    sid: str = SESSION_ID,
    prompt: str = SECRET,
) -> dict:
    return {
        "ts": iso(moment),
        "session_id": sid,
        "prompt": prompt,
        "hits": {"session": 1, "trace": 1, "graph_context": 0} if hits is None else hits,
        "per_scope": per_scope
        or {
            "session": {"hits": 1, "elapsed_ms": 900.0},
            "graph": {"hits": 0, "elapsed_ms": 0, "skipped": True},
        },
        "context": context,
    }


def write_jsonl(path: pathlib.Path, *rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows),
        encoding="utf-8",
    )


def context_of(*items: str) -> str:
    return (
        "Relevant context from this session's memory:\n\n=== Prior agent trace ===\n"
        + "\n\n".join(items)
    )


@pytest.fixture
def env(tmp_path):
    state, claude = tmp_path / "state", tmp_path / "claude"
    state.mkdir()
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "cognee-memory@cognee": [
                        {
                            "version": "2.0.0",
                            "installedAt": "2026-08-01T00:00:00.000Z",
                            "lastUpdated": "2026-09-12T00:00:00.000Z",
                            "gitCommitSha": "abcdef0123456789",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    class Env:
        plugin_dir, claude_dir = state, claude
        transcript = claude / "projects" / "proj" / (SESSION_UUID + ".jsonl")

        def args(self, *extra):
            return pe.build_parser().parse_args(
                ["report", "--plugin-dir", str(state), "--claude-dir", str(claude)] + list(extra)
            )

        def card(self, since=None, until=None, *extra):
            return pe.build_scorecard(
                self.args(*extra), since, until, pe.load_install_info(claude), "test"
            )

    return Env()


# --------------------------------------------------------------------------- dates


def test_parse_when_accepts_every_documented_form():
    now = ts(17, 12)
    install = {
        "last_updated": "2026-09-12T00:00:00+00:00",
        "installed_at": "2026-08-01T00:00:00+00:00",
    }
    assert pe.parse_when("all", now, install) is None
    assert pe.parse_when("24h", now, install) == now - timedelta(hours=24)
    assert pe.parse_when("90m", now, install) == now - timedelta(minutes=90)
    assert pe.parse_when("7d", now, install) == now - timedelta(days=7)
    assert pe.parse_when("2w", now, install) == now - timedelta(days=14)
    assert pe.parse_when("last-update", now, install) == datetime(2026, 9, 12, tzinfo=UTC)
    assert pe.parse_when("installed", now, install) == datetime(2026, 8, 1, tzinfo=UTC)
    assert pe.parse_when("2026-09-10T08:30:00Z", now, install) == datetime(
        2026, 9, 10, 8, 30, tzinfo=UTC
    )
    # A bare date is LOCAL midnight; as --until it means the end of that day.
    local_midnight = datetime(2026, 9, 10).astimezone().astimezone(UTC)
    assert pe.parse_when("2026-09-10", now, install) == local_midnight
    assert pe.parse_when("2026-09-10", now, install, end_of_day=True) == local_midnight + timedelta(
        days=1
    )


def test_parse_when_explains_bad_input_and_missing_registry():
    with pytest.raises(pe.WhenError, match="YYYY-MM-DD"):
        pe.parse_when("last tuesday-ish", ts(17), {})
    with pytest.raises(pe.WhenError, match="installed_plugins.json"):
        pe.parse_when("last-update", ts(17), {})


def test_parse_ts_handles_z_suffix_odd_fractions_and_naive_utc():
    assert pe.parse_ts("2026-09-17T15:12:28.4Z") == datetime(
        2026, 9, 17, 15, 12, 28, 400000, tzinfo=UTC
    )
    assert pe.parse_ts("2026-09-17T15:12:28") == datetime(2026, 9, 17, 15, 12, 28, tzinfo=UTC)
    assert pe.parse_ts("not a date") is None and pe.parse_ts(None) is None


def test_rotated_files_are_read_oldest_first_and_locks_ignored(tmp_path):
    for name in ("hook.log", "hook.log.1", "hook.log.2", "hook.log.lock"):
        (tmp_path / name).write_text("", encoding="utf-8")
    assert [p.name for p in pe.rotated_files(tmp_path, "hook.log")] == [
        "hook.log.2",
        "hook.log.1",
        "hook.log",
    ]


# --------------------------------------------------------------------------- hook.log


def test_window_filter_and_reliability_accounting(env):
    write_jsonl(
        env.plugin_dir / "hook.log.1", hook(ts(1), "trace_stored", status="success")
    )  # before the window
    write_jsonl(
        env.plugin_dir / "hook.log",
        hook(ts(10), "trace_stored", status="success"),
        hook(ts(10), "stop_stored", chars=10),
        hook(ts(10), "store_buffered_warming", hook="tool"),
        hook(ts(10), "store_buffered_warming", hook="stop"),
        hook(ts(10), "warmup_drained", count=1),
        hook(ts(10), "trace_store_error"),
        hook(ts(10), "improve_fired", ok=True, busy=False, trigger="idle"),
        hook(
            ts(10),
            "improve_fired",
            ok=False,
            busy=False,
            trigger="final",
            error="The read operation timed out",
        ),
        hook(ts(10), "improve_fired", ok=False, busy=True, trigger="idle"),
        hook(ts(10), "recall_error", pid=7, scope="session", verdict="slow"),
        hook(
            ts(10), "recall_error", pid=7, scope="trace", verdict="slow"
        ),  # same recall, second scope
        hook(ts(10), "recall_budget_exceeded", pid=7, collected=3),
        hook(ts(10), "skip_self_cognee_bash", cmd_prefix="ls ~/cognee"),
        "{this line is not json",
    )
    report = pe.analyze_hook_log(env.plugin_dir, ts(5), None)
    stores = report["stores"]
    assert (
        stores["stored_directly"],
        stores["buffered"],
        stores["errors"],
        stores["drained_later"],
    ) == (2, 2, 1, 1)
    assert stores["attempted"] == 5 and stores["landed_rate"] == pytest.approx(3 / 5)
    assert stores["skipped_as_self_reference"] == 1 and stores["self_skip_share"] == pytest.approx(
        1 / 6
    )
    assert (report["improve"]["ok"], report["improve"]["failed"], report["improve"]["busy"]) == (
        1,
        1,
        1,
    )
    assert report["improve"]["ok_rate"] == 0.5  # a busy answer is not a failure
    assert report["improve"]["top_errors"] == [("The read operation timed out", 1)]
    assert (
        report["recall_events"]["errors"] == 2
        and report["recall_events"]["recalls_with_error"] == 1
    )
    assert report["recall_events"]["recalls_over_budget"] == 1
    assert report["unparseable_lines"] == 1
    assert report["first_ts"] == iso(
        ts(1)
    )  # coverage is reported even for lines outside the window


# --------------------------------------------------------------------------- recalls


def test_reinjection_is_primed_by_rows_before_the_window(env):
    write_jsonl(
        env.plugin_dir / "recall-audit.log",
        recall(ts(1), context_of(ITEM_A)),  # before the window: primes "already injected"
        recall(ts(10), context_of(ITEM_A, ITEM_B)),  # A is a repeat, B is new
        recall(ts(11), context_of(ITEM_A, ITEM_B)),  # everything is a repeat
    )
    result, per_session = pe.analyze_recalls(env.plugin_dir, ts(5), None, None)
    a, b = len(pe.normalize_text(ITEM_A)), len(pe.normalize_text(ITEM_B))
    assert result["recalls"] == 2 and list(per_session) == [SESSION_ID]
    assert result["reinjection"]["items"] == 4 and result["reinjection"]["repeated_items"] == 3
    assert result["reinjection"]["fully_repeated_recalls"] == 1
    assert result["reinjection"]["repeated_char_share"] == pytest.approx(
        (2 * a + b) / (2 * a + 2 * b)
    )
    assert result["novelty_rate"] == pytest.approx(b / (2 * a + 2 * b))


def test_empty_injections_graph_share_and_scope_latency(env):
    write_jsonl(
        env.plugin_dir / "recall-audit.log",
        recall(
            ts(10),
            context_of(ITEM_A),
            hits={"session": 3, "graph_context": 1},
            per_scope={
                "session": {"hits": 3, "elapsed_ms": 1200.0},
                "graph": {"hits": 1, "elapsed_ms": 400.0},
            },
        ),
        recall(
            ts(11),
            "Cognee memory: nothing relevant was found for this prompt, carry on.",
            hits={"session": 0, "graph_context": 0},
        ),
    )
    result, _ = pe.analyze_recalls(env.plugin_dir, None, None, None)
    assert result["hit_rate"] == 0.5 and result["empty_injection_rate"] == 0.5
    assert result["graph_hit_share"] == pytest.approx(1 / 4)
    assert (
        result["graph_skipped_rate"] == 0.5
    )  # the second row used the default per_scope (graph skipped)
    assert result["scope_latency_ms"]["session"]["p95"] == 1200.0
    assert result["slowest_scope_ms"]["n"] == 2


def test_session_filter_accepts_bare_uuid(env):
    write_jsonl(
        env.plugin_dir / "recall-audit.log",
        recall(ts(10), context_of(ITEM_A)),
        recall(ts(10), context_of(ITEM_B), sid="claude_other"),
    )
    result, _ = pe.analyze_recalls(env.plugin_dir, None, None, SESSION_UUID)
    assert result["recalls"] == 1


# --------------------------------------------------------------------------- transcripts


def _attachment(moment, **fields):
    return {
        "type": "attachment",
        "timestamp": iso(moment).replace("+00:00", "Z"),
        "attachment": fields,
    }


def _assistant(moment, message_id, content, fresh=100, cached=1000, out=10):
    return {
        "type": "assistant",
        "timestamp": iso(moment).replace("+00:00", "Z"),
        "message": {
            "id": message_id,
            "content": content,
            "usage": {
                "input_tokens": fresh,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cached,
                "output_tokens": out,
            },
        },
    }


def _user(moment, content):
    return {
        "type": "user",
        "timestamp": iso(moment).replace("+00:00", "Z"),
        "message": {"role": "user", "content": content},
    }


PEP604 = (
    "Traceback (most recent call last):\n  File "
    '"/home/u/.claude/plugins/cache/cognee/cognee-memory/1.4.4/scripts/'
    'clear-transcript-context.py", line 28, in <module>\nTypeError: unsupported operand type(s) '
    "for |: 'type' and 'NoneType'"
)


def test_transcript_usage_is_deduplicated_and_hooks_are_attributed(env):
    write_jsonl(
        env.transcript,
        _attachment(
            ts(10, 9),
            type="hook_success",
            hookEvent="SessionStart",
            command="Initializing Cognee memory...",
            durationMs=9000,
        ),
        _attachment(
            ts(10, 9),
            type="hook_success",
            hookEvent="PreToolUse",
            command="if [ -z $HOME ]; then echo other-tool; fi",
            durationMs=5,
        ),
        # one API response written as two lines: usage must be counted once
        _assistant(ts(10, 10), "msg_1", [{"type": "text", "text": "hello"}]),
        _assistant(
            ts(10, 10),
            "msg_1",
            [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "ls ~/.cognee-plugin/claude-code/"},
                }
            ],
        ),
        _assistant(
            ts(10, 11),
            "msg_2",
            [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "bash ${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh 'x'"},
                },
                {
                    "type": "tool_use",
                    "name": "Skill",
                    "input": {"skill": "cognee-memory:cognee-remember"},
                },
            ],
        ),
        _attachment(
            ts(10, 12),
            type="hook_non_blocking_error",
            hookEvent="Stop",
            command="Clearing Claude context...",
            stderr=PEP604,
            exitCode=1,
            durationMs=300,
        ),
    )
    data = pe.analyze_transcript(env.transcript, None, None, [])
    assert data["usage"]["input_tokens"] == 200 and data["api_messages"] == 2
    assert data["skills"] == {
        "script:cognee-search.sh": 1,
        "cognee-memory:cognee-remember": 1,
    }  # `ls` of the state dir is not use
    assert [(r["event"], r["ok"], r["version"]) for r in data["hook_runs"]] == [
        ("SessionStart", True, ""),
        ("Stop", False, "1.4.4"),
    ]
    assert "clear-transcript-context.py: TypeError" in data["hook_runs"][1]["signature"]


def _stop_summary(moment, *errors):
    """The per-turn record Claude Code writes: every Stop hook that ran, and every error."""
    infos = [
        {"command": 'if [ -z "${HOME-}" ]; then echo other-tool; fi', "durationMs": 90},
        {"command": "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/store-to-session.py --stop || python x"},
        {"command": "Clearing Claude context...", "durationMs": 150},
    ]
    return {
        "type": "system",
        "subtype": "stop_hook_summary",
        "timestamp": iso(moment).replace("+00:00", "Z"),
        "hookCount": len(infos),
        "hookInfos": infos,
        "hookErrors": list(errors),
    }


def test_stop_hook_runs_come_from_the_turn_summary_not_the_error_attachment(env):
    """A Stop hook that succeeds leaves no attachment, so attachments alone read as 100% crashed."""
    failing = "Failed with non-blocking status code: " + PEP604
    write_jsonl(
        env.transcript,
        _stop_summary(ts(10, 10), failing),
        # Claude Code also writes the same failure as an attachment: it must not count twice
        _attachment(
            ts(10, 10),
            type="hook_non_blocking_error",
            hookEvent="Stop",
            command="Clearing Claude context...",
            stderr=PEP604,
            exitCode=1,
        ),
        _stop_summary(ts(10, 11)),
        _stop_summary(ts(10, 12)),
    )
    data = pe.analyze_transcript(env.transcript, None, None, [])
    runs = [(r["hook"], r["ok"], r["version"]) for r in data["hook_runs"]]
    assert runs.count(("clear-transcript-context.py", False, "1.4.4")) == 1
    assert runs.count(("clear-transcript-context.py", True, "")) == 2
    assert runs.count(("store-to-session.py", True, "")) == 3  # the other tool's hook is ignored
    assert len(runs) == 6

    write_jsonl(env.plugin_dir / "recall-audit.log", recall(ts(10, 9), context_of(ITEM_A)))
    transcripts = env.card()["transcripts"]
    assert transcripts["per_hook"]["clear-transcript-context.py"] == {"ok": 2, "failed": 1}
    assert (transcripts["hook_runs"], transcripts["hook_crashes"]) == (6, 1)
    assert transcripts["hooks"]["Stop"]["p50_ms"] == 150.0
    crash = next(a for a in env.card()["advice"] if a["id"] == "hook_crash:Stop")
    assert "1 of 3 runs failed" in crash["evidence"]  # rated per hook, not across all Stop hooks
    assert "clear-transcript-context.py" in crash["title"]


def test_in_transcript_detection_respects_time_and_compaction(env):
    body = "the deployment checklist says rotate the staging database credentials every quarter"
    later = "pytest reported forty two passed and three skipped in the payments integration suite"
    write_jsonl(
        env.transcript,
        _user(
            ts(10, 10),
            [{"type": "tool_result", "content": [{"type": "text", "text": "Output:\n" + body}]}],
        ),
        _user(
            ts(10, 12), [{"type": "tool_result", "content": later}]
        ),  # arrives AFTER the first recall
        {
            "type": "system",
            "subtype": "compact_boundary",
            "timestamp": iso(ts(10, 13)).replace("+00:00", "Z"),
        },
        _user(ts(10, 14), "a fresh prompt after compaction"),
    )
    items = pe.split_items(context_of(ITEM_A, ITEM_B))
    before_compaction = pe.analyze_transcript(
        env.transcript, None, None, [{"ts": ts(10, 11), "items": items, "chars": 1}]
    )
    assert before_compaction["in_transcript"]["checked_items"] == 2
    assert before_compaction["in_transcript"]["items"] == 1  # A was visible; B only appears later
    after_compaction = pe.analyze_transcript(
        env.transcript, None, None, [{"ts": ts(10, 15), "items": items, "chars": 1}]
    )
    assert (
        after_compaction["in_transcript"]["items"] == 0
    )  # compaction removed both from the live context
    assert after_compaction["compactions"] == 1


# --------------------------------------------------------------------------- grade + advice


def test_grade_excludes_low_n_and_withholds_letter_when_a_gate_fails():
    values = {key: (None, 0) for key, *_ in pe.METRIC_SPECS}
    values.update(
        {
            "store_landed_rate": (0.99, 50),
            "hit_rate": (0.9, 50),
            "novelty_rate": (0.9, 50),
            "median_injected_tokens": (400, 50),
            "recall_p95_ms": (1000, 50),
            "improve_ok_rate": (0.0, 2),
        }
    )  # terrible, but only 2 observations
    graded = pe.compute_grade(values)
    assert graded["overall"]["status"] == "graded" and graded["overall"]["letter"] == "A"
    assert next(m for m in graded["metrics"] if m["key"] == "improve_ok_rate")["status"] == "low_n"

    values["hit_rate"] = (0.4, 50)  # below the 60% gate
    failed = pe.compute_grade(values)
    assert failed["overall"]["status"] == "gates_failed" and failed["overall"]["letter"] is None
    assert (
        failed["overall"]["failed_gates"] == ["hit_rate"] and failed["overall"]["score"] is not None
    )

    sparse = pe.compute_grade({key: (None, 0) for key, *_ in pe.METRIC_SPECS})
    assert sparse["overall"]["status"] == "insufficient_data"


def _seed_crashing_stop_hook(env, runs: int = 12):
    write_jsonl(env.plugin_dir / "recall-audit.log", recall(ts(14), context_of(ITEM_A)))
    write_jsonl(
        env.transcript,
        *[
            _attachment(
                ts(14, 10, i),
                type="hook_non_blocking_error",
                hookEvent="Stop",
                command="Clearing Claude context...",
                stderr=PEP604,
                exitCode=1,
            )
            for i in range(runs)
        ],
    )


def test_hook_crash_is_p0_on_the_installed_version_and_demoted_once_superseded(env):
    _seed_crashing_stop_hook(env)
    superseded = next(a for a in env.card()["advice"] if a["id"] == "hook_crash:Stop")
    assert (
        superseded["priority"] == "P2"
        and "Seen only under v1.4.4; v2.0.0 is installed now" in superseded["action"]
    )
    assert "from __future__ import annotations" in superseded["action"]

    registry = env.claude_dir / "plugins" / "installed_plugins.json"
    registry.write_text(
        registry.read_text(encoding="utf-8").replace("2.0.0", "1.4.4"), encoding="utf-8"
    )
    current = next(a for a in env.card()["advice"] if a["id"] == "hook_crash:Stop")
    assert current["priority"] == "P0" and "12 of 12 runs failed" in current["evidence"]
    assert env.card()["grade"]["overall"]["failed_gates"] == ["hook_crash_rate"]


def test_self_reference_filter_and_stale_update_notice(env):
    write_jsonl(env.plugin_dir / "recall-audit.log", recall(ts(14), context_of(ITEM_A)))
    write_jsonl(
        env.plugin_dir / "hook.log",
        *[hook(ts(14), "skip_self_cognee_bash", cmd_prefix="ls") for _ in range(30)],
        hook(ts(14), "trace_stored", status="success"),
        hook(ts(14), "update_check", installed="1.4.4", latest="2.0.0", available=True),
    )
    ids = [a["id"] for a in env.card()["advice"]]
    assert "trace_filter_too_broad" in ids
    assert "update_available" not in ids  # the registry already says 2.0.0 is installed


def test_compare_is_direction_aware(env):
    write_jsonl(
        env.plugin_dir / "recall-audit.log",
        *[
            recall(ts(3, 10, i), context_of(ITEM_A), hits={"session": 0}) for i in range(25)
        ],  # baseline: no hits
        *[recall(ts(12, 10, i), context_of(ITEM_B), hits={"session": 2}) for i in range(25)],
    )  # after: all hit
    current = env.card(ts(10), ts(15), "--no-transcripts")
    baseline = env.card(ts(5) - timedelta(days=5), ts(10), "--no-transcripts")
    rows = {row["key"]: row for row in pe.compare_cards(current, baseline)["rows"]}
    assert (rows["hit_rate"]["before"], rows["hit_rate"]["after"], rows["hit_rate"]["verdict"]) == (
        0.0,
        1.0,
        "better",
    )
    assert rows["empty_injection_rate"]["verdict"] == "better"  # lower is better for this one
    assert rows["improve_ok_rate"]["verdict"] == "n/a"


# --------------------------------------------------------------------------- CLI + privacy


def _run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = pe.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_cli_json_report_exit_codes_and_markers(env):
    write_jsonl(
        env.plugin_dir / "recall-audit.log",
        recall(ts(10), context_of(ITEM_A)),
        recall(ts(14), context_of(ITEM_B)),
    )
    base = ["--plugin-dir", str(env.plugin_dir), "--claude-dir", str(env.claude_dir)]
    code, out, _ = _run(["report", "--since", "last-update", "--format", "json"] + base)
    card = json.loads(out)
    assert code == 0 and card["schema"] == pe.SCHEMA_VERSION and card["recall"]["recalls"] == 1
    assert (
        card["window"]["since"] == "2026-09-12T00:00:00+00:00"
        and card["install"]["version"] == "2.0.0"
    )

    assert (
        _run(["--since", "2026-09-01", "--format", "json"] + base)[0] == 0
    )  # `report` is the default command
    assert _run(["report", "--since", "whenever"] + base)[0] == 2
    assert _run(["report", "--compare"] + base)[0] == 2  # nothing to compare "all time" against
    assert _run(["report", "--since", "2026-09-10", "--until", "2026-09-01"] + base)[0] == 2
    assert (
        _run(
            [
                "report",
                "--plugin-dir",
                str(env.plugin_dir / "nope"),
                "--claude-dir",
                str(env.claude_dir),
            ]
        )[0]
        == 3
    )

    code, out, _ = _run(["markers"] + base)
    presets = {p["value"]: p for p in json.loads(out)["presets"]}
    assert code == 0 and presets["last-update"]["recalls"] == 1 and presets["all"]["recalls"] == 2


def test_reports_and_notion_payload_never_contain_prompt_or_memory_text(env, tmp_path):
    write_jsonl(
        env.plugin_dir / "recall-audit.log",
        *[recall(ts(14, 10, i), context_of(ITEM_A, SECRET + " " + SECRET)) for i in range(25)],
    )
    write_jsonl(
        env.transcript,
        _user(ts(14, 9), SECRET),
        _assistant(ts(14, 9), "m", [{"type": "text", "text": SECRET}]),
    )
    base = ["--plugin-dir", str(env.plugin_dir), "--claude-dir", str(env.claude_dir)]
    payload = tmp_path / "notion.json"
    for fmt in ("text", "markdown", "json"):
        code, out, _ = _run(["report", "--format", fmt, "--notion-dry-run", str(payload)] + base)
        assert code == 0 and "zebra-quartz" not in out and "staging database" not in out
    assert "zebra-quartz" not in payload.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- Notion


def test_parse_notion_id_from_urls_and_ids():
    expected = "3d537007-fa82-80f9-8e4a-e65b44fae5e9"
    assert (
        pe.parse_notion_id(
            "https://app.notion.com/p/acme/Onboarding-Checklist-3d537007fa8280f98e4ae65b44fae5e9?source=copy_link"
        )
        == expected
    )
    # a title ending in hex letters must not shift the id window
    assert (
        pe.parse_notion_id("https://www.notion.so/acme/Facade-3d537007fa8280f98e4ae65b44fae5e9")
        == expected
    )
    assert (
        pe.parse_notion_id(expected) == expected
        and pe.parse_notion_id(expected.replace("-", "").upper()) == expected
    )
    assert pe.parse_notion_id("https://www.notion.so/acme/No-Id-Here") is None


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, message: str, retry_after: str = "") -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://api.notion.com",
        code,
        "err",
        headers,
        io.BytesIO(json.dumps({"message": message}).encode()),
    )


def _big_card(env):
    write_jsonl(env.plugin_dir / "recall-audit.log", recall(ts(14), context_of(ITEM_A)))
    card = env.card()
    card["advice"] = [
        {
            "id": "x%d" % i,
            "kind": "pain",
            "priority": "P1",
            "title": "t%d" % i,
            "evidence": "e",
            "why": "w",
            "action": "a" * 4500,
            "impact": 1.0,
        }
        for i in range(40)
    ]
    return card


def test_notion_export_chunks_blocks_splits_long_text_and_retries_rate_limits(env):
    calls, sleeps, outcomes = [], [], [_http_error(429, "slow down", "2")]

    def opener(request, timeout):
        calls.append(
            (
                request.get_method(),
                request.full_url,
                json.loads(request.data),
                dict(request.header_items()),
            )
        )
        if outcomes:
            raise outcomes.pop(0)
        return _Response(json.dumps({"id": "page-1", "url": "https://notion.so/page-1"}).encode())

    page = pe.export_to_notion(
        _big_card(env),
        "secret-token",
        "3d537007fa8280f98e4ae65b44fae5e9",
        opener=opener,
        sleep=sleeps.append,
    )
    assert sleeps == [2.0] and page["url"] == "https://notion.so/page-1" and page["blocks"] > 100
    methods = [(method, url.rsplit("/v1", 1)[1]) for method, url, _body, _headers in calls]
    assert methods[:2] == [
        ("POST", "/pages"),
        ("POST", "/pages"),
    ]  # the 429 is retried, then succeeds
    assert all(m == ("PATCH", "/blocks/page-1/children") for m in methods[2:]) and len(methods) >= 3
    create = calls[1][2]
    assert (
        create["parent"] == {"page_id": "3d537007-fa82-80f9-8e4a-e65b44fae5e9"}
        and "title" in create["properties"]
    )
    for _method, _url, body, headers in calls:
        assert len(body["children"]) <= pe.NOTION_MAX_CHILDREN
        assert (
            headers["Authorization"] == "Bearer secret-token"
            and headers["Notion-version"] == pe.NOTION_DEFAULT_VERSION
        )
        for block in body["children"]:
            for text in block[block["type"]].get("rich_text", []):
                assert len(text["text"]["content"]) <= pe.NOTION_MAX_TEXT
    assert sum(len(body["children"]) for _m, _u, body, _h in calls[1:]) == page["blocks"]


def test_notion_errors_are_actionable_and_never_echo_the_token(env):
    card = _big_card(env)

    def not_shared(request, timeout):
        raise _http_error(404, "Could not find page")

    with pytest.raises(pe.NotionError) as raised:
        pe.export_to_notion(
            card,
            "secret-token",
            "3d537007fa8280f98e4ae65b44fae5e9",
            opener=not_shared,
            sleep=lambda s: None,
        )
    assert "share the parent page with your integration" in str(
        raised.value
    ) and "secret-token" not in str(raised.value)
    with pytest.raises(pe.NotionError, match="NOTION_TOKEN is not set"):
        pe.export_to_notion(card, "", "3d537007fa8280f98e4ae65b44fae5e9")
    with pytest.raises(pe.NotionError, match="could not find a page id"):
        pe.export_to_notion(card, "secret-token", "https://www.notion.so/acme/No-Id-Here")


def test_server_join_dedupes_runs_and_does_not_double_count_child_tokens():
    rows = [
        {
            "id": "a1",
            "pipeline_run_id": "run-1",
            "pipeline_name": "cognify_pipeline",
            "status": "DATASET_PROCESSING_STARTED",
            "created_at": "2026-09-14T10:00:00",
            "tokens_in": None,
            "tokens_out": None,
        },
        {
            "id": "a2",
            "pipeline_run_id": "run-1",
            "pipeline_name": "cognify_pipeline",
            "status": "DATASET_PROCESSING_COMPLETED",
            "outcome": "succeeded",
            "created_at": "2026-09-14T10:01:00",
            "tokens_in": 700,
            "tokens_out": 99,
            "parent_operation_id": "op-1",
        },
        {
            "id": "b1",
            "pipeline_run_id": "op-1",
            "pipeline_name": None,
            "operation_name": "improve",
            "outcome": "succeeded",
            "created_at": "2026-09-14T10:01:01",
            "tokens_in": 700,
            "tokens_out": 99,
        },
        {
            "id": "c1",
            "pipeline_run_id": "op-2",
            "pipeline_name": None,
            "operation_name": "remember",
            "outcome": "succeeded",
            "background": True,
            "created_at": "2026-09-14T10:02:00",
            "tokens_in": 0,
            "tokens_out": 0,
        },
        {
            "id": "old",
            "pipeline_run_id": "op-0",
            "pipeline_name": None,
            "operation_name": "recall",
            "outcome": "succeeded",
            "created_at": "2026-09-01T10:00:00",
            "tokens_in": 5,
            "tokens_out": 5,
        },
    ]
    server = pe.analyze_server(
        "https://tenant.example",
        "key",
        ts(10),
        None,
        opener=lambda request, timeout: _Response(json.dumps(rows).encode()),
    )
    assert server["available"] and server["runs"] == 3
    assert server["pipelines"] == {"cognify_pipeline": {"completed": 1}}
    assert server["operations"] == {
        "improve": {"succeeded": 1},
        "remember": {"accepted_in_background": 1},
    }
    assert (server["tokens_in"], server["tokens_out"]) == (
        700,
        99,
    )  # the child's tokens are already in its parent
    assert pe.analyze_server("", "key", None, None)["available"] is False


def test_bash_capture_report_counts_all_decisions(env):
    write_jsonl(
        env.plugin_dir / "hook.log",
        *[
            hook(ts(10), "bash_capture_decision", host_session_id=SESSION_UUID, outcome=outcome)
            for outcome in ("captured", "skipped_self", "skipped_low_signal", "captured")
        ],
    )
    card = env.card()
    counts = card["bash_capture"][SESSION_UUID]
    assert counts == {"captured": 2, "skipped_self": 1, "skipped_low_signal": 1}
    assert sum(counts.values()) == 4
    text = pe.render_text(card)
    assert "2 traces captured" in text
    assert "1 skipped-self" in text and "1 skipped-low-signal" in text


def test_value_report_order_bars_gates_and_full_layout(env):
    write_jsonl(env.plugin_dir / "recall-audit.log", recall(ts(10), context_of(ITEM_A)))
    card = env.card()
    # Exercise six simultaneous failures without changing the evaluator's rules.
    for i, metric in enumerate(card["grade"]["metrics"]):
        metric["status"] = "fail" if i < 6 else "low_n"
        metric["value"] = 0.123
        metric["n"] = 50 if i < 6 else 1
    card["grade"]["overall"].update(
        status="gates_failed",
        score=42,
        failed_gates=[m["label"] for m in card["grade"]["metrics"][:6]],
    )
    card["grade"]["subscores"] = dict.fromkeys(pe.SUBSCORES, 82)
    text = pe.render_text(card)
    sections = [
        "What Cognee did for you",
        "Where it helped",
        "Quality",
        "NO GRADE",
    ]
    positions = [text.index(section) for section in sections]
    assert positions == sorted(positions)
    assert SESSION_ID in text and ITEM_A not in text and SECRET not in text
    assert "Needs attention" not in text
    for metric in card["grade"]["metrics"]:
        assert metric["label"] in text
        assert pe.fmt_value(metric["value"], metric["unit"]) in text
    assert text.count("[████████░░] 82") == 4
    ascii_text = pe.render_text(card, ascii=True)
    assert ascii_text.count("[########--] 82") == 4
    markdown = pe.render_text(card, markdown=True)
    fenced = markdown.split("```")[1]
    assert "[████████░░] 82 reliability" in fenced
    assert "latency" not in fenced
    assert "Needs attention" not in markdown
    assert "Needs attention" not in ascii_text
    assert text.rstrip().endswith(pe.grade_headline(card))
    assert "low-n" in text
    assert pe.render_text(card, full=True) == pe.render_full_text(card)
    assert (
        pe.render_text(card, full=True).split("Recommendations (evidence-backed)")[1]
        == pe.render_full_text(card).split("Recommendations (evidence-backed)")[1]
    )


def test_delivery_segments_partition_recalls_and_receipts_are_body_free(env):
    rows = [
        recall(ts(10, 10), context_of(ITEM_A), hits={"graph_context": 1}),
        recall(ts(10, 11), context_of(ITEM_A), hits={"graph_context": 1}),
        recall(ts(10, 12), context_of(ITEM_B), hits={"session": 1}),
        recall(ts(10, 13), "", hits={}),
        {**recall(ts(10, 14), "", hits={}), "entries_deduped": 2},
    ]
    write_jsonl(env.plugin_dir / "recall-audit.log", *rows)
    receipt_dir = env.plugin_dir / "sessions" / SESSION_UUID
    receipt_dir.mkdir(parents=True)
    (receipt_dir / "injection-receipt.json").write_text(
        json.dumps(
            {
                "recorded_at": iso(ts(10)),
                "items": [{"reason": "injected", "hash": "a" * 64, "text": SECRET}],
            }
        )
    )
    card = env.card(ts(10, 0), ts(11, 0))
    assert card["delivery"]["segments"] == dict.fromkeys(pe._DELIVERY_SEGMENTS, 1)
    assert sum(card["delivery"]["segments"].values()) == card["recall"]["recalls"]
    assert card["delivery"]["label"] == "delivery quality"
    assert card["delivery"]["sessions"][0]["evidence_hashes"] == ["a" * 64]
    assert SECRET not in json.dumps(card["delivery"])
    # A later receipt must not be projected back onto an earlier window.
    old = env.card(ts(9, 0), ts(10, 10))
    assert old["delivery"]["sessions"] == []


def test_default_and_full_cli_flags(env):
    args = [
        "report",
        "--plugin-dir",
        str(env.plugin_dir),
        "--claude-dir",
        str(env.claude_dir),
        "--no-transcripts",
    ]
    code, compact, _ = _run(args + ["--ascii"])
    assert code == 0 and "What Cognee did for you" in compact and "[----------]" in compact
    code, full, _ = _run(args + ["--full"])
    assert code == 0 and "Recommendations (evidence-backed)" in full
    assert "What Cognee did for you" not in full


def test_default_report_survives_a_non_utf8_stdout(tmp_path):
    # Windows pipes default to cp1252 on Python 3.9; the quality bars must not crash it.
    import os
    import subprocess

    script = pathlib.Path(_scripts_dir()) / "plugin_eval.py"
    plugin_dir = tmp_path / ".cognee-plugin" / "claude-code"
    plugin_dir.mkdir(parents=True)
    # Path.home() reads USERPROFILE on Windows, HOME elsewhere: point both at the temp home.
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "PYTHONIOENCODING": "cp1252",
    }
    result = subprocess.run(
        [sys.executable, str(script), "report", "--since", "7d", "--no-transcripts"],
        env=env,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"Traceback" not in result.stderr
