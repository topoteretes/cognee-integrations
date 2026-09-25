"""PreToolUse(Read) file context (``file-context.py``, Claude Code only).

The hook is additive and must be cheap, so the tests pin its two contracts:

* the gates — nothing is fetched (and nothing printed) for a disabled hook, a
  non-file Read, a sensitive path, a path already served this session, a
  server known to be down, or a file no lane can say anything about;
* the rendering — the code-graph facts collapse into a compact map (symbols by
  kind with line numbers, cross-file calls, imports) and come back in the
  documented ``hookSpecificOutput.additionalContext`` shape.

Everything the hook would ask the server is faked at ``recall_via_http``; the
indexed-repo lookup is faked at ``_code_graph.find_indexed_repo``.
"""

from __future__ import annotations

import json
import sys

import pytest

_SESSION = "host-session-1"


@pytest.fixture
def fc(suite, hook_module):
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: file-context.py is a Claude Code hook")
    return hook_module(suite, "file-context.py")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    return root


def _facts(rel="pkg/mod.py"):
    stem = rel.rsplit(".", 1)[0]
    return [
        {
            "kind": "file_ref",
            "name": rel,
            "file": rel,
            "line": 1,
            "relations": [{"type": "calls", "target": "pkg/log.get_logger"}],
        },
        {
            "kind": "symbol",
            "name": f"{stem}.Widget",
            "line": 10,
            "properties": {"symbol_kind": "class", "exported": True},
            "relations": [
                {"type": "calls", "target": f"{stem}._helper"},  # in-file: not "calls out"
                {"type": "calls", "target": "pkg/util.normalize"},
                {"type": "has_method", "target": f"{stem}.Widget.run"},
            ],
        },
        {
            "kind": "symbol",
            "name": f"{stem}.Widget.run",
            "line": 14,
            "properties": {"symbol_kind": "method", "exported": True},
            "relations": [],
        },
        {
            "kind": "symbol",
            "name": f"{stem}._helper",
            "line": 3,
            "properties": {"symbol_kind": "function", "exported": False},
            "relations": [],
        },
        {"kind": "dependency", "name": f"{stem} -> os", "line": 1},
        {"kind": "dependency", "name": f"{stem} -> pkg.util", "line": 2},
    ]


def _payload(path, cwd=""):
    return {
        "session_id": _SESSION,
        "cwd": str(cwd),
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": str(path)},
    }


@pytest.fixture
def wire(fc, repo, monkeypatch, isolated_modules, suite):
    """Fake the indexed repo + server; return (calls, run) where calls records recalls."""
    # Each isolated_modules() call pops every isolated module first, so the one
    # the hook imports lazily (_code_graph) must be loaded LAST to stay patched.
    pc = isolated_modules(suite, "_plugin_common")
    code_graph = isolated_modules(suite, "_code_graph")
    calls: list[dict] = []
    state = {"repo_root": str(repo), "dataset": "codebase-repo-1234"}
    monkeypatch.setattr(
        code_graph, "find_indexed_repo", lambda cwd: state if str(cwd).startswith(str(repo)) else {}
    )

    def fake_recall(query, **kw):
        calls.append({"query": query, **kw})
        if kw.get("scope") == ["code"]:
            return [{"source": "code", "raw": {"operation": "query_facts", "facts": _facts()}}]
        return [{"source": "graph", "text": "We decided Widget.run must stay idempotent."}]

    monkeypatch.setattr(fc, "recall_via_http", fake_recall)
    monkeypatch.setattr(
        fc,
        "load_resolved",
        lambda *a, **k: {"session_id": "cognee-sess", "dataset": "agent_sessions"},
    )
    monkeypatch.setattr(fc, "read_connection_state", lambda: {})
    monkeypatch.setattr(pc, "read_connection_state", lambda: {})
    return calls


# ── rendering ────────────────────────────────────────────────────────────────


def test_code_facts_render_as_compact_map(fc):
    block = fc.format_code_facts(_facts(), "pkg/mod.py")
    lines = block.splitlines()
    assert lines[0] == "Symbols in pkg/mod.py (name:line):"
    # Kinds in a stable order, names shortened to the file, private flagged.
    assert "  classes: Widget:10" in lines
    assert "  functions: _helper:3 (private)" in lines
    assert "  methods: Widget.run:14" in lines
    # Only cross-file calls are listed, including module-level (file_ref) ones.
    calls = next(line for line in lines if line.startswith("Calls out to:"))
    assert "pkg/util (normalize)" in calls
    assert "pkg/log (get_logger)" in calls
    assert "_helper" not in calls
    assert "Imports: os, pkg.util" in lines


def test_code_facts_cap_symbols(fc):
    facts = [
        {
            "kind": "symbol",
            "name": f"pkg/mod.f{i}",
            "line": i,
            "properties": {"symbol_kind": "function"},
        }
        for i in range(1, 51)
    ]
    block = fc.format_code_facts(facts, "pkg/mod.py", max_symbols=10)
    assert "f10:10" in block and "f11:11" not in block
    assert "… 40 more symbols not shown" in block


def test_empty_facts_render_nothing(fc):
    assert fc.format_code_facts([], "pkg/mod.py") == ""
    assert fc.format_code_facts([{"kind": "file_ref", "name": "pkg/mod.py"}], "pkg/mod.py") == ""


def test_facts_from_accepts_raw_or_text(fc):
    facts = _facts()
    via_raw = fc.facts_from([{"raw": {"facts": facts}}])
    via_text = fc.facts_from([{"text": json.dumps({"facts": facts})}])
    assert len(via_raw) == len(via_text) == len(facts)
    assert fc.facts_from([{"text": "not json"}, "junk", None]) == []


# ── the hook end to end (in-process) ─────────────────────────────────────────


def test_read_in_indexed_repo_injects_code_context(fc, repo, wire, capsys, monkeypatch):
    _run(fc, monkeypatch, _payload(repo / "pkg" / "mod.py", cwd=repo))
    out = json.loads(capsys.readouterr().out)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso  # additive only: never blocks the read
    assert hso["additionalContext"].startswith("## Cognee: about mod.py")
    assert "Widget:10" in hso["additionalContext"]
    # The code lane queried the repo's OWN dataset, filtered to this file.
    (call,) = wire
    assert call["dataset"] == "codebase-repo-1234"
    assert call["code_query"] == {
        "operation": "query_facts",
        "file": "pkg/mod.py",
        "limit": fc.CODE_LIMIT,
    }


def test_same_file_is_served_once_per_ttl(fc, repo, wire, capsys, monkeypatch):
    payload = _payload(repo / "pkg" / "mod.py", cwd=repo)
    _run(fc, monkeypatch, payload)
    assert capsys.readouterr().out.strip()
    _run(fc, monkeypatch, payload)
    assert capsys.readouterr().out == ""
    assert len(wire) == 1


def test_failed_lookup_does_not_mark_file_seen(fc, repo, wire, capsys, monkeypatch):
    """A timeout/error is not an answer: the next Read of the file tries again."""
    real = fc.recall_via_http
    attempts: list[int] = []

    def flaky(query, **kw):
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("read deadline exhausted")
        return real(query, **kw)

    monkeypatch.setattr(fc, "recall_via_http", flaky)
    payload = _payload(repo / "pkg" / "mod.py", cwd=repo)
    _run(fc, monkeypatch, payload)
    assert capsys.readouterr().out == ""
    _run(fc, monkeypatch, payload)
    assert (
        "Widget:10"
        in json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    )
    assert len(attempts) == 2


def test_empty_answer_still_marks_file_seen(fc, repo, wire, capsys, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(fc, "recall_via_http", lambda q, **kw: calls.append(1) or [])
    payload = _payload(repo / "pkg" / "mod.py", cwd=repo)
    _run(fc, monkeypatch, payload)
    _run(fc, monkeypatch, payload)
    assert capsys.readouterr().out == ""
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("offset", "stale"),
    [(-100.0, True), (+100.0, False), (None, False)],
    ids=["edited-after-index", "indexed-after-edit", "no-index-stamp"],
)
def test_edit_after_index_flags_line_numbers(fc, repo, wire, capsys, monkeypatch, offset, stale):
    """mtime past ``last_index_at`` adds a shifted-lines note; unknown adds nothing."""
    target = repo / "pkg" / "mod.py"
    mtime = target.stat().st_mtime
    state = {"repo_root": str(repo), "dataset": "codebase-repo-1234"}
    if offset is not None:
        state["last_index_at"] = mtime + offset
    monkeypatch.setattr(sys.modules["_code_graph"], "find_indexed_repo", lambda cwd: state)
    _run(fc, monkeypatch, _payload(target, cwd=repo))
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "Widget:10" in ctx  # the map is still served either way
    assert ("line numbers may have shifted" in ctx) is stale


def test_edited_since_index_tolerates_bad_inputs(fc, tmp_path):
    f = tmp_path / "a.py"
    f.write_text("", encoding="utf-8")
    assert not fc.edited_since_index(str(f), None)
    assert not fc.edited_since_index(str(f), "garbage")
    assert not fc.edited_since_index(str(tmp_path / "missing.py"), 1.0)
    assert fc.edited_since_index(str(f), 1.0)


def test_seen_marker_expires(fc, monkeypatch):
    fc.mark_seen(_SESSION, "/a/b.py", now=1000.0)
    assert fc.recently_seen(_SESSION, "/a/b.py", ttl=100.0, now=1050.0)
    assert not fc.recently_seen(_SESSION, "/a/b.py", ttl=100.0, now=1200.0)
    assert not fc.recently_seen(_SESSION, "/other.py", ttl=100.0, now=1050.0)


@pytest.mark.parametrize(
    ("env", "path_name"),
    [
        ({"COGNEE_FILE_CONTEXT": "false"}, "pkg/mod.py"),
        ({}, ".env"),  # sensitive: never even asked about
        ({}, "secrets/id_rsa"),
    ],
)
def test_gates_fetch_nothing(fc, repo, wire, monkeypatch, capsys, env, path_name):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _run(fc, monkeypatch, _payload(repo / path_name, cwd=repo))
    assert capsys.readouterr().out == ""
    assert wire == []


def test_file_outside_indexed_repo_has_no_lane(fc, tmp_path, wire, capsys, monkeypatch):
    other = tmp_path / "elsewhere" / "x.py"
    other.parent.mkdir()
    other.write_text("", encoding="utf-8")
    _run(fc, monkeypatch, _payload(other))
    assert capsys.readouterr().out == ""
    assert wire == []


def test_known_down_server_is_not_asked(fc, repo, wire, monkeypatch, capsys):
    monkeypatch.setattr(fc, "read_connection_state", lambda: {"state": "unreachable"})
    _run(fc, monkeypatch, _payload(repo / "pkg" / "mod.py", cwd=repo))
    assert capsys.readouterr().out == ""
    assert wire == []


def test_graph_lane_is_opt_in(fc, repo, wire, monkeypatch, capsys):
    monkeypatch.setenv("COGNEE_FILE_CONTEXT_SCOPES", "code,graph")
    _run(fc, monkeypatch, _payload(repo / "pkg" / "mod.py", cwd=repo))
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "Memory about this file:" in ctx
    assert "Widget.run must stay idempotent" in ctx
    scopes = [c["scope"] for c in wire]
    assert scopes == [["code"], ["graph"]]
    assert wire[1]["search_type"] == "HYBRID_COMPLETION"
    assert wire[1]["dataset"] == "agent_sessions"


def test_non_read_tool_and_bad_payload_are_ignored(fc, wire, capsys, monkeypatch):
    _run(fc, monkeypatch, {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    _run(fc, monkeypatch, "{not json")
    _run(fc, monkeypatch, "")
    assert capsys.readouterr().out == ""
    assert wire == []


def test_observer_child_exits_before_reading(fc, repo, wire, monkeypatch, capsys):
    """Inside the observer's own ``claude -p`` child no hook may call the server."""
    monkeypatch.setenv("COGNEE_OBSERVER_CHILD", "1")
    _run(fc, monkeypatch, _payload(repo / "pkg" / "mod.py", cwd=repo))
    assert capsys.readouterr().out == ""
    assert wire == []


def test_file_path_resolves_relative_to_cwd(fc):
    # normpath uses the platform separator (\\w\\a\\b.py on Windows).
    norm = fc.os.path.normpath
    assert fc.file_path_from({"cwd": "/w", "tool_input": {"file_path": "a/b.py"}}) == norm(
        "/w/a/b.py"
    )
    assert fc.file_path_from({"tool_input": {"file_path": "/abs/x.py"}}) == norm("/abs/x.py")
    assert fc.file_path_from({"tool_input": {}}) == ""
    assert fc.file_path_from({"tool_input": "nope"}) == ""


def _run(fc, monkeypatch, payload) -> None:
    monkeypatch.setattr(fc.sys, "stdin", _Stdin(payload))
    fc.main()


class _Stdin:
    def __init__(self, payload):
        self._text = payload if isinstance(payload, str) else json.dumps(payload)

    def read(self):
        return self._text
