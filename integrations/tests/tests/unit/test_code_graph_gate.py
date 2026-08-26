"""The pure decision logic of the code-graph lane: what fires it and what must not.

The auto-recall code lane runs on the keystroke->answer path, so its gate is
deliberately syntactic (identifier-shaped token) rather than semantic. These
tests pin the two halves of that contract:

  * conversational prompts produce NO identifiers, so the lane never fires and
    ordinary turns pay nothing;
  * a prompt naming a symbol produces a seed, but the lane still stays off
    unless the cwd sits inside a repository the user explicitly indexed.

The transport half (submitting an index, re-ingesting on change) lives in
integration/test_code_graph.py.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def cg(suite, isolated_modules):
    return isolated_modules(suite, "_code_graph")


# ── the identifier gate ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "prompt",
    [
        "can you explain what this does",
        "why did that fail?",
        "let's think about the plan for tomorrow",
        "thanks, that worked",
        "",
    ],
)
def test_conversational_prompts_do_not_arm_the_lane(cg, prompt):
    """No identifier -> no seed -> the lane must not fire at all."""
    assert cg.extract_identifiers(prompt) == []


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("what calls process_payment?", "process_payment"),
        ("explain UserService please", "UserService"),
        ("look at billing/api.py", "billing/api.py"),
        ("check `resolve_repo_source` for me", "resolve_repo_source"),
        ("where is cognee.modules.retrieval used", "cognee.modules.retrieval"),
    ],
)
def test_identifier_shapes_are_recognized(cg, prompt, expected):
    assert expected in cg.extract_identifiers(prompt)


def test_prose_camelcase_is_not_a_seed(cg):
    """Product/tool names appear in ordinary prose; seeding on them would fire
    the lane on nearly every prompt in this ecosystem."""
    assert cg.extract_identifiers("does Claude know about Cognee and GitHub?") == []


def test_identifiers_are_capped_and_deduped(cg):
    found = cg.extract_identifiers(
        "process_payment and process_payment and refund_order and audit_log"
    )
    assert len(found) <= 2
    assert len(set(found)) == len(found)


def test_code_query_is_a_bounded_fact_lookup(cg):
    """query_facts (not explore) on purpose: an unresolvable seed must return an
    empty page rather than raise, so a misfire can never disturb the prompt."""
    q = cg.build_code_query("process_payment")
    assert q["operation"] == "query_facts"
    assert q["name"] == "process_payment"
    assert isinstance(q["limit"], int) and q["limit"] > 0


# ── the indexed-repo requirement ───────────────────────────────────────────


def test_lane_stays_off_without_an_indexed_repo(cg, tmp_path):
    """An identifier alone is not enough: nobody asked for this repo to be indexed."""
    assert cg.auto_code_lane("what calls process_payment?", str(tmp_path)) == {}


def test_lane_arms_inside_an_indexed_repo(cg, tmp_path):
    repo = tmp_path / "myrepo"
    (repo / "src").mkdir(parents=True)
    cg.save_repo_state(
        {
            "spec": str(repo),
            "spec_kind": "path",
            "repo_root": str(repo),
            "dataset": "codebase-myrepo",
            "fingerprint": "abc",
        }
    )

    lane = cg.auto_code_lane("what calls process_payment?", str(repo / "src"))
    assert lane["dataset"] == "codebase-myrepo"
    assert lane["identifier"] == "process_payment"
    assert lane["code_query"]["operation"] == "query_facts"


def test_lane_stays_off_for_conversational_prompt_in_indexed_repo(cg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cg.save_repo_state(
        {
            "spec": str(repo),
            "spec_kind": "path",
            "repo_root": str(repo),
            "dataset": "codebase-myrepo",
        }
    )
    assert cg.auto_code_lane("thanks, that looks good", str(repo)) == {}


def test_nested_checkouts_resolve_to_the_innermost_repo(cg, tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "vendor" / "inner"
    inner.mkdir(parents=True)
    for root, dataset in ((outer, "codebase-outer"), (inner, "codebase-inner")):
        cg.save_repo_state(
            {
                "spec": str(root),
                "spec_kind": "path",
                "repo_root": str(root),
                "dataset": dataset,
            }
        )
    lane = cg.auto_code_lane("check UserService", str(inner))
    assert lane["dataset"] == "codebase-inner"


def test_sibling_directory_is_not_inside_the_repo(cg, tmp_path):
    """Prefix matching must respect path boundaries: /repo-other is not in /repo."""
    repo = tmp_path / "repo"
    other = tmp_path / "repo-other"
    repo.mkdir()
    other.mkdir()
    cg.save_repo_state(
        {
            "spec": str(repo),
            "spec_kind": "path",
            "repo_root": str(repo),
            "dataset": "codebase-repo",
        }
    )
    assert cg.auto_code_lane("check UserService", str(other)) == {}


# ── dataset naming ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "spec,prefix",
    [
        ("/path/to/my-repo", "codebase-my-repo-"),
        ("https://github.com/org/cognee.git", "codebase-cognee-"),
        ("/path/to/Repo_Name", "codebase-repo_name-"),
    ],
)
def test_default_dataset_is_readable_and_narrow(cg, spec, prefix):
    """One dataset per repo — the CODE snapshot cache and the delta pre-read
    both scale with dataset size — and the name stays recognizable in a
    dataset listing despite the disambiguating digest."""
    name = cg.default_code_dataset(spec)
    assert name.startswith(prefix)
    assert len(name) > len(prefix)  # a digest follows


def test_default_dataset_is_stable_across_calls(cg):
    """Re-indexing must target the same dataset, not mint a new one."""
    assert cg.default_code_dataset("/path/to/repo") == cg.default_code_dataset("/path/to/repo")


def test_same_basename_repos_get_different_datasets(cg):
    """The collision that matters: distinct checkouts routinely share a
    basename. Landing both in one dataset means one graph database, where
    cognee's repo-scoped stale-node sweep would let each ingestion delete the
    other's nodes — silent, mutual data loss."""
    a = cg.default_code_dataset("/work/team-a/service")
    b = cg.default_code_dataset("/work/team-b/service")
    assert a != b
    assert a.startswith("codebase-service-") and b.startswith("codebase-service-")


def test_same_basename_remote_repos_get_different_datasets(cg):
    """Basenames collide across hosts and orgs too."""
    assert cg.default_code_dataset("https://github.com/org-a/api") != cg.default_code_dataset(
        "https://github.com/org-b/api"
    )


def test_git_suffix_and_trailing_slash_do_not_fork_a_dataset(cg):
    """The same remote written three ways is one repository, not three."""
    base = cg.default_code_dataset("https://github.com/org/repo")
    assert cg.default_code_dataset("https://github.com/org/repo.git") == base
    assert cg.default_code_dataset("https://github.com/org/repo/") == base


def test_state_files_do_not_collide_for_same_basename(cg, tmp_path):
    """The dataset name is not the only identity in play: two same-named
    checkouts must also keep separate state files, or the second index would
    overwrite the first's fingerprint and dataset."""
    for parent in ("team-a", "team-b"):
        root = tmp_path / parent / "service"
        root.mkdir(parents=True)
        cg.save_repo_state(
            {
                "spec": str(root),
                "spec_kind": "path",
                "repo_root": str(root),
                "dataset": cg.default_code_dataset(str(root)),
            }
        )
    states = cg.load_repo_states()
    assert len(states) == 2
    assert len({state["dataset"] for state in states}) == 2


@pytest.mark.parametrize(
    "spec,remote",
    [
        ("https://github.com/org/repo", True),
        ("git@github.com:org/repo.git", True),
        ("/local/path/repo", False),
        (".", False),
    ],
)
def test_remote_repo_detection(cg, spec, remote):
    assert cg.is_remote_repo(spec) is remote


# ── dataset resolution for callers ─────────────────────────────────────────


def test_dataset_resolves_from_a_checkout(cg, tmp_path):
    """Dataset names carry a path digest, so no caller can reconstruct one.
    They resolve it from the checkout instead — including from a subdirectory,
    which is where agents usually run."""
    repo = tmp_path / "service"
    (repo / "src").mkdir(parents=True)
    dataset = cg.default_code_dataset(str(repo))
    cg.save_repo_state(
        {
            "spec": str(repo),
            "spec_kind": "path",
            "repo_root": str(repo),
            "dataset": dataset,
        }
    )
    assert cg.find_indexed_repo(str(repo / "src"))["dataset"] == dataset


def test_dataset_is_empty_outside_an_indexed_repo(cg, tmp_path):
    """No index, no dataset — the caller must not fall back to the session
    dataset and search prose as if it were code."""
    assert cg.find_indexed_repo(str(tmp_path)) == {}


# ── recall arg plumbing ────────────────────────────────────────────────────


@pytest.fixture
def rh(suite, isolated_modules):
    return isolated_modules(suite, "_recall_http")


def test_malformed_code_query_degrades_to_no_lane(rh):
    """A bad code_query must never become a server 422 that reads as a recall
    failure — it simply drops the lane."""
    assert rh.coerce_code_query("not json") is None
    assert rh.coerce_code_query("") is None
    assert rh.coerce_code_query("[1,2]") is None
    assert rh.coerce_code_query(None) is None


def test_code_query_accepts_json_and_dicts(rh):
    parsed = rh.coerce_code_query(json.dumps({"operation": "delta"}))
    assert parsed == {"operation": "delta"}
    assert rh.coerce_code_query({"operation": "explore"}) == {"operation": "explore"}
