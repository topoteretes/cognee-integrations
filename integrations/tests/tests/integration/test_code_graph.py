"""The code-graph wire contract, driven against the mock Cognee server.

Three things a real server has to confirm, because getting any of them wrong
fails silently rather than loudly:

  * an index submits ``content_type=code`` with the repo in ``repositories``
    and NO file upload — the server rejects the two together, and a plain
    upload would build a per-file graph instead of a repo one;
  * a code file remembered with ``--file`` keeps its REAL filename, because
    the extension is the server's loader-routing signal (a ``.txt`` rename
    silently ingests code as prose through the LLM pipeline);
  * the freshness pass re-submits only when the working tree actually changed,
    and keeps the old fingerprint when a submission fails so the next turn
    retries instead of skipping.

The pure gate logic lives in unit/test_code_graph_gate.py.
"""

from __future__ import annotations

import json
import subprocess

import pytest

REMEMBER = "/api/v1/remember"
RECALL = "/api/v1/recall"


@pytest.fixture
def cg(suite, isolated_modules):
    return isolated_modules(suite, "_code_graph")


@pytest.fixture
def rh(suite, isolated_modules):
    return isolated_modules(suite, "_remember_http")


@pytest.fixture
def git_repo(tmp_path):
    """A real git repo — the freshness gate shells out to git for its fingerprint."""
    repo = tmp_path / "proj"
    repo.mkdir()

    def git(*args):
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", "commit.gpgsign", "false")
    (repo / "app.py").write_text("def start():\n    return 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    return repo


# ── indexing a repository ──────────────────────────────────────────────────


def test_index_submits_repositories_not_a_file_upload(cg, mock_server):
    """content_type=code + repositories, with no `data` file part.

    The server rejects uploads combined with content_type='code', and an upload
    without it would build a one-file graph with no cross-file edges.
    """
    res = cg.do_index_repo(mock_server.url, "", "/path/to/proj", "codebase-proj")
    assert res["ok"] is True

    call = mock_server.assert_called("POST", REMEMBER)
    assert call["form"]["content_type"] == "code"
    assert call["form"]["repositories"] == "/path/to/proj"
    assert call["form"]["datasetName"] == "codebase-proj"
    assert call["form"]["run_in_background"] == "true"
    assert call.get("files", []) == []


def test_index_vectors_is_off_by_default(cg, mock_server):
    """The graph-only default needs no embedding provider; opting in is explicit."""
    cg.do_index_repo(mock_server.url, "", "/path/to/proj", "ds")
    assert mock_server.assert_called("POST", REMEMBER)["form"]["index_vectors"] == "false"

    mock_server.calls.clear()
    cg.do_index_repo(mock_server.url, "", "/path/to/proj", "ds", index_vectors=True)
    assert mock_server.assert_called("POST", REMEMBER)["form"]["index_vectors"] == "true"


def test_api_key_header_attached(cg, mock_server):
    cg.do_index_repo(mock_server.url, "cloud-key", "https://github.com/org/repo", "ds")
    assert mock_server.assert_called("POST", REMEMBER)["headers"].get("X-Api-Key") == "cloud-key"


def test_old_server_rejection_names_the_version_requirement(cg, mock_server):
    """A pre-1.5.3 server 400s content_type='code'. The message must say so —
    otherwise it reads as a bad repo path and sends the user hunting."""
    mock_server.force_response(
        "POST", REMEMBER, 400, {"detail": "Unsupported content_type 'code'."}
    )
    res = cg.do_index_repo(mock_server.url, "", "/path/to/proj", "ds")
    assert res != cg.UNREACHABLE
    assert "1.5.3" in res["error"]


@pytest.mark.parametrize("code", [401, 403, 500])
def test_http_error_is_not_unreachable(cg, mock_server, code):
    """Reachable-but-failing must stay distinguishable from absent: there is no
    CLI fallback for this route, so a wrong verdict here just loses the error."""
    mock_server.force_response("POST", REMEMBER, code, {"detail": "nope"})
    res = cg.do_index_repo(mock_server.url, "", "/path/to/proj", "ds")
    assert res != cg.UNREACHABLE
    assert res["status"] == code


def test_unparseable_body_is_not_success(cg, mock_server):
    """A 2xx we cannot parse must not read as a confirmed write.

    Unlike the fire-and-forget remember path, callers here advance the repo's
    stored fingerprint on "ok" — so reporting success for a body that proves
    nothing would mark edits as indexed that may never have been.
    """
    mock_server.force_response("POST", REMEMBER, 200, b"<html>502 Bad Gateway</html>")
    res = cg.do_index_repo(mock_server.url, "", "/path/to/proj", "ds")
    assert res != cg.UNREACHABLE  # reachable server: no CLI-fallback semantics
    assert isinstance(res, dict) and "error" in res
    assert res.get("ok") is not True


def test_unparseable_body_does_not_advance_the_fingerprint(cg, mock_server, git_repo):
    """The consequence that actually matters: the turn's edits stay pending.

    If the fingerprint advanced here, the next turn would see an unchanged
    tree, skip the re-index, and leave the code graph silently stale with no
    error anywhere. Keeping the old fingerprint costs one re-submission, which
    the server's content hash skips when the write did land.
    """
    cg.index_repo(mock_server.url, "", str(git_repo))
    before = cg.load_repo_states()[0]["fingerprint"]

    (git_repo / "app.py").write_text("def start():\n    return 99\n", encoding="utf-8")
    mock_server.force_response("POST", REMEMBER, 200, b"not json at all")

    outcome = cg.reingest_if_changed(str(git_repo), mock_server.url, "")
    assert outcome["changed"] is True and outcome["submitted"] is False
    assert cg.load_repo_states()[0]["fingerprint"] == before


def test_unparseable_body_leaves_the_repo_unregistered(cg, mock_server, git_repo):
    """An explicit index that could not be confirmed must not look indexed:
    a state file would enable the recall lane and the freshness loop for a
    graph we have no evidence exists."""
    mock_server.force_response("POST", REMEMBER, 200, b"<html>oops</html>")
    res = cg.index_repo(mock_server.url, "", str(git_repo))
    assert "error" in res
    assert cg.load_repo_states() == []


def test_unreachable_server_is_the_sentinel(cg):
    res = cg.do_index_repo("http://127.0.0.1:1", "", "/path/to/proj", "ds")
    assert res == cg.UNREACHABLE


def test_index_records_state_for_a_local_repo(cg, mock_server, git_repo):
    """Indexing is the freshness opt-in: without a recorded state the hooks
    must never touch a repo."""
    res = cg.index_repo(mock_server.url, "", str(git_repo))
    assert res["ok"] is True
    # Readable prefix plus a path digest — the digest is what stops two
    # same-named checkouts from sharing a dataset (and a graph database).
    assert res["dataset"].startswith("codebase-proj-")

    states = cg.load_repo_states()
    assert len(states) == 1
    assert states[0]["repo_root"] == str(git_repo.resolve())
    assert states[0]["spec_kind"] == "path"
    assert states[0]["fingerprint"]  # a real tree fingerprint was captured


def test_url_indexed_repo_records_no_fingerprint(cg, mock_server):
    """A server-side clone only ever sees pushed commits, so there is no local
    tree to track — and the Stop hook must not pretend otherwise."""
    cg.index_repo(mock_server.url, "", "https://github.com/org/repo")
    state = cg.load_repo_states()[0]
    assert state["spec_kind"] == "url"
    assert state["fingerprint"] == ""


# ── freshness ──────────────────────────────────────────────────────────────


def test_unchanged_tree_submits_nothing(cg, mock_server, git_repo):
    cg.index_repo(mock_server.url, "", str(git_repo))
    mock_server.calls.clear()

    outcome = cg.reingest_if_changed(str(git_repo), mock_server.url, "")
    assert outcome["changed"] is False
    mock_server.assert_not_called("POST", REMEMBER)


def test_edited_tree_resubmits_and_advances_the_fingerprint(cg, mock_server, git_repo):
    cg.index_repo(mock_server.url, "", str(git_repo))
    before = cg.load_repo_states()[0]["fingerprint"]
    mock_server.calls.clear()

    (git_repo / "app.py").write_text("def start():\n    return 2\n", encoding="utf-8")
    outcome = cg.reingest_if_changed(str(git_repo), mock_server.url, "")

    assert outcome["changed"] is True and outcome["submitted"] is True
    assert mock_server.assert_called("POST", REMEMBER)["form"]["content_type"] == "code"
    assert cg.load_repo_states()[0]["fingerprint"] != before


def test_untracked_file_counts_as_a_change(cg, mock_server, git_repo):
    """porcelain alone would miss content edits; new files must count too."""
    cg.index_repo(mock_server.url, "", str(git_repo))
    (git_repo / "extra.py").write_text("x = 1\n", encoding="utf-8")
    assert cg.reingest_if_changed(str(git_repo), mock_server.url, "")["changed"] is True


def test_failed_resubmit_keeps_the_old_fingerprint(cg, mock_server, git_repo):
    """Advancing on failure would mark the turn's edits as indexed when they
    were not — the graph would stay stale until the NEXT edit."""
    cg.index_repo(mock_server.url, "", str(git_repo))
    before = cg.load_repo_states()[0]["fingerprint"]

    (git_repo / "app.py").write_text("def start():\n    return 3\n", encoding="utf-8")
    mock_server.force_response("POST", REMEMBER, 500, {"detail": "boom"})

    outcome = cg.reingest_if_changed(str(git_repo), mock_server.url, "")
    assert outcome["changed"] is True and outcome["submitted"] is False
    assert cg.load_repo_states()[0]["fingerprint"] == before


# ── retry backoff on repeated failures ─────────────────────────────────────


def _fail(mock_server, status=401):
    mock_server.force_response("POST", REMEMBER, status, {"detail": "nope"})


def test_repeated_failures_stop_hitting_the_network(cg, mock_server, git_repo):
    """A failed submission leaves the fingerprint alone, so the tree stays
    'changed' on every later turn — conversation-only turns included. Without a
    throttle one unresolved failure re-submits once per turn forever and
    appends to a hook log that never rotates."""
    cg.index_repo(mock_server.url, "", str(git_repo))
    (git_repo / "app.py").write_text("def start():\n    return 2\n", encoding="utf-8")
    _fail(mock_server)
    mock_server.calls.clear()

    for _ in range(6):
        cg.reingest_if_changed(str(git_repo), mock_server.url, "")

    posts = [c for c in mock_server.calls if c["method"] == "POST" and c["path"] == REMEMBER]
    assert len(posts) == 1, f"expected one attempt inside the window, got {len(posts)}"


def test_backoff_never_advances_the_fingerprint(cg, mock_server, git_repo):
    """Throttling must not be implemented by marking the edits done: the
    pending work has to survive the quiet period, or the graph goes silently
    stale the moment the underlying problem is fixed."""
    cg.index_repo(mock_server.url, "", str(git_repo))
    before = cg.load_repo_states()[0]["fingerprint"]
    (git_repo / "app.py").write_text("def start():\n    return 3\n", encoding="utf-8")
    _fail(mock_server)

    for _ in range(4):
        cg.reingest_if_changed(str(git_repo), mock_server.url, "")

    assert cg.load_repo_states()[0]["fingerprint"] == before


def test_throttled_turns_report_nothing_to_log(cg, mock_server, git_repo):
    """The suppressed turn returns the 'nothing to do' shape, which the Stop
    hook does not log — that is what actually stops the log noise."""
    cg.index_repo(mock_server.url, "", str(git_repo))
    (git_repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _fail(mock_server)

    first = cg.reingest_if_changed(str(git_repo), mock_server.url, "")
    second = cg.reingest_if_changed(str(git_repo), mock_server.url, "")
    assert first["submitted"] is False and "retry_in" in first
    assert second == {}


def test_attempt_resumes_once_the_window_expires(cg, mock_server, git_repo):
    """Backoff delays recovery, it must not prevent it."""
    cg.index_repo(mock_server.url, "", str(git_repo))
    (git_repo / "app.py").write_text("x = 2\n", encoding="utf-8")
    _fail(mock_server)
    cg.reingest_if_changed(str(git_repo), mock_server.url, "")

    state = cg.load_repo_states()[0]
    state["last_error_at"] = 0.1  # window long past
    cg.save_repo_state(state)
    mock_server.clear_forced()

    outcome = cg.reingest_if_changed(str(git_repo), mock_server.url, "")
    assert outcome["submitted"] is True


def test_success_clears_the_backoff(cg, mock_server, git_repo):
    """A recovered repo must not carry a stale penalty into its next failure."""
    cg.index_repo(mock_server.url, "", str(git_repo))
    (git_repo / "app.py").write_text("x = 3\n", encoding="utf-8")
    _fail(mock_server)
    cg.reingest_if_changed(str(git_repo), mock_server.url, "")
    assert cg.load_repo_states()[0]["error_count"] == 1

    state = cg.load_repo_states()[0]
    state["last_error_at"] = 0.1
    cg.save_repo_state(state)
    mock_server.clear_forced()
    cg.reingest_if_changed(str(git_repo), mock_server.url, "")

    assert "error_count" not in cg.load_repo_states()[0]


def test_backoff_escalates_and_is_capped(cg):
    """Escalating keeps a long outage quiet; the cap bounds how late a fixed
    credential is noticed."""
    schedule = [cg.retry_backoff_seconds(n) for n in range(1, 8)]
    assert schedule[0] == 30
    assert schedule == sorted(schedule)
    assert max(schedule) == cg._RETRY_BACKOFF_MAX_SECONDS
    assert cg.retry_backoff_seconds(0) == 0


def test_backwards_clock_does_not_park_a_repo_in_backoff(cg, mock_server, git_repo):
    """A system clock moved backwards must not strand a repo forever."""
    cg.index_repo(mock_server.url, "", str(git_repo))
    (git_repo / "app.py").write_text("x = 4\n", encoding="utf-8")
    _fail(mock_server)
    cg.reingest_if_changed(str(git_repo), mock_server.url, "")

    state = cg.load_repo_states()[0]
    state["last_error_at"] = 9_999_999_999.0  # far future
    cg.save_repo_state(state)
    mock_server.calls.clear()

    cg.reingest_if_changed(str(git_repo), mock_server.url, "")
    assert any(c["path"] == REMEMBER for c in mock_server.calls)


def test_explicit_index_ignores_the_backoff(cg, mock_server, git_repo):
    """An explicit request is the user asking directly — it must never be
    silently swallowed by a throttle they cannot see."""
    cg.index_repo(mock_server.url, "", str(git_repo))
    (git_repo / "app.py").write_text("x = 5\n", encoding="utf-8")
    _fail(mock_server)
    cg.reingest_if_changed(str(git_repo), mock_server.url, "")
    mock_server.clear_forced()
    mock_server.calls.clear()

    res = cg.index_repo(mock_server.url, "", str(git_repo))
    assert res["ok"] is True
    assert any(c["path"] == REMEMBER for c in mock_server.calls)


def test_unindexed_repo_is_left_alone(cg, mock_server, git_repo):
    """No opt-in, no traffic: the hooks never index a repo nobody asked for."""
    assert cg.reingest_if_changed(str(git_repo), mock_server.url, "") == {}
    mock_server.assert_not_called("POST", REMEMBER)


def test_url_indexed_repo_is_not_resubmitted_from_the_working_tree(cg, mock_server, git_repo):
    cg.index_repo(mock_server.url, "", "https://github.com/org/repo", "codebase-proj")
    # Point the URL-indexed state at a local root to prove spec_kind, not the
    # presence of a root, is what gates the local freshness path.
    state = cg.load_repo_states()[0]
    state["repo_root"] = str(git_repo.resolve())
    cg.save_repo_state(state)
    mock_server.calls.clear()

    (git_repo / "app.py").write_text("changed\n", encoding="utf-8")
    assert cg.reingest_if_changed(str(git_repo), mock_server.url, "") == {}
    mock_server.assert_not_called("POST", REMEMBER)


def test_non_git_directory_is_skipped_quietly(cg, mock_server, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    cg.save_repo_state(
        {
            "spec": str(plain),
            "spec_kind": "path",
            "repo_root": str(plain),
            "dataset": "codebase-plain",
            "fingerprint": "old",
        }
    )
    assert cg.reingest_if_changed(str(plain), mock_server.url, "") == {}
    mock_server.assert_not_called("POST", REMEMBER)


# ── the indexer's own output must not re-trigger indexing ─────────────────


def _write_enola_snapshot(repo, marker: str) -> None:
    """Mimic what `enola --generate` leaves in the indexed repo: an untracked
    .enola/ dir whose files (and rotated `previous/` copies) change every run."""
    snap = repo / ".enola"
    (snap / "previous").mkdir(parents=True, exist_ok=True)
    (snap / "receipt.json").write_text('{"snapshot_id": "%s"}' % marker, encoding="utf-8")
    (snap / "facts.jsonl").write_text(marker * 50, encoding="utf-8")
    (snap / "previous" / f"receipt-{marker}.json").write_text("{}", encoding="utf-8")


def test_enola_snapshot_dir_does_not_change_the_fingerprint(cg, git_repo):
    """enola writes its snapshot INTO the repo it indexes and rotates files
    there on every run. If that were visible to the fingerprint, every index
    would make the next turn look 'changed' and re-index — a full parse per
    turn, forever, for every locally indexed repo."""
    before = cg.git_fingerprint(str(git_repo))
    _write_enola_snapshot(git_repo, "run1")
    assert cg.git_fingerprint(str(git_repo)) == before
    _write_enola_snapshot(git_repo, "run2")  # a second run rewrites/rotates
    assert cg.git_fingerprint(str(git_repo)) == before


def test_enola_snapshot_dir_does_not_trigger_a_reindex(cg, mock_server, git_repo):
    cg.index_repo(mock_server.url, "", str(git_repo))
    mock_server.calls.clear()
    _write_enola_snapshot(git_repo, "run1")  # the background index landed

    outcome = cg.reingest_if_changed(str(git_repo), mock_server.url, "")
    assert outcome["changed"] is False
    mock_server.assert_not_called("POST", REMEMBER)


def test_real_source_edits_still_register_next_to_a_snapshot(cg, git_repo):
    """Excluding .enola must not blind the fingerprint to actual edits."""
    _write_enola_snapshot(git_repo, "run1")
    before = cg.git_fingerprint(str(git_repo))
    (git_repo / "app.py").write_text("def start():\n    return 7\n", encoding="utf-8")
    assert cg.git_fingerprint(str(git_repo)) != before


def _find_enola():
    import glob
    import os
    import pwd
    import shutil

    candidates = [os.environ.get("ENOLA_PATH", ""), shutil.which("enola") or ""]
    try:
        real_home = pwd.getpwuid(os.getuid()).pw_dir  # tests redirect HOME
        candidates += sorted(glob.glob(os.path.join(real_home, ".cognee", "bin", "enola-*")))
    except Exception:
        pass
    return next((c for c in candidates if c and os.access(c, os.X_OK)), "")


@pytest.mark.skipif(not _find_enola(), reason="enola binary not installed locally")
def test_fingerprint_is_stable_across_real_enola_runs(cg, git_repo):
    """The definitive check against the real binary, not a mimic of its
    output: two generates with no source edit must leave the fingerprint
    exactly where it was before the first one."""
    enola = _find_enola()
    before = cg.git_fingerprint(str(git_repo))
    for _ in range(2):
        subprocess.run(
            [enola, "--generate"], cwd=git_repo, check=True, capture_output=True, timeout=180
        )
        assert cg.git_fingerprint(str(git_repo)) == before
    assert (git_repo / ".enola").is_dir(), "enola did not write its snapshot where expected"


# ── per-file code uploads (the .txt-rename fix) ────────────────────────────


def test_file_upload_keeps_its_real_filename(rh, mock_server, tmp_path):
    """The filename extension IS the routing signal: `payments.py` rides the
    zero-LLM code route, `user_context.txt` would be ingested as prose."""
    src = tmp_path / "payments.py"
    src.write_text("def charge():\n    pass\n", encoding="utf-8")

    res = rh.do_remember(mock_server.url, "", "", "ds", "project_docs", file_path=str(src))
    assert res["ok"] is True

    call = mock_server.assert_called("POST", REMEMBER)
    assert call["files"] == ["data"]
    # content_type must NOT be set: per-file routing is by extension, and
    # content_type='code' would reject the upload outright.
    assert "content_type" not in call["form"]


def test_inline_text_still_uses_the_node_set_filename(rh, mock_server):
    """The default path is unchanged — only --file opts into real filenames."""
    res = rh.do_remember(mock_server.url, "", "a fact", "ds", "user_context")
    assert res["ok"] is True
    assert mock_server.assert_called("POST", REMEMBER)["files"] == ["data"]


def test_missing_file_is_an_error_not_a_write(rh, mock_server, tmp_path):
    res = rh.do_remember(
        mock_server.url, "", "", "ds", "project_docs", file_path=str(tmp_path / "nope.py")
    )
    assert res != rh.UNREACHABLE
    assert "cannot read" in res["error"]
    mock_server.assert_not_called("POST", REMEMBER)


# ── recall plumbing ────────────────────────────────────────────────────────


@pytest.fixture
def rc(suite, isolated_modules):
    return isolated_modules(suite, "_recall_http")


def test_code_query_reaches_the_wire_with_the_code_scope(rc, mock_server):
    rc.do_recall(
        mock_server.url,
        "",
        "process_payment",
        "",
        json.dumps(["code"]),
        5,
        "codebase-proj",
        "",
        json.dumps({"operation": "impact_analysis", "targets": ["process_payment"]}),
    )
    body = mock_server.assert_called("POST", RECALL)["json"]
    assert body["scope"] == ["code"]
    assert body["code_query"]["operation"] == "impact_analysis"
    assert body["datasets"] == ["codebase-proj"]


@pytest.fixture
def client(suite, isolated_modules):
    return isolated_modules(suite, "_cognee_client")


def test_breaker_wrapped_recall_forwards_the_code_query(client, mock_server):
    """The breaker wrapper sits between the CLI wrapper and do_recall, whose
    signature has context_profile BETWEEN dataset and code_query — a positional
    hand-off there silently ships the query as a context profile, and the lane
    returns unfiltered results instead of failing."""
    client.recall(
        mock_server.url,
        "",
        "process_payment",
        "",
        json.dumps(["code"]),
        10,
        "codebase-proj",
        json.dumps({"operation": "impact_analysis", "targets": ["process_payment"]}),
    )
    body = mock_server.assert_called("POST", RECALL)["json"]
    assert body["code_query"]["operation"] == "impact_analysis"
    assert "context_profile" not in body


def test_absent_code_query_is_omitted_entirely(rc, mock_server):
    """The server rejects code_query without the code scope, so an unused lane
    must not leave the key behind on ordinary recalls."""
    rc.do_recall(mock_server.url, "", "hello", "", json.dumps(["graph"]), 5, "ds")
    assert "code_query" not in mock_server.assert_called("POST", RECALL)["json"]
