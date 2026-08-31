"""Tests for the "is this marker about my server?" predicate, which exists twice.

`_plugin_common.same_connection_target` decides whether a *hook* may record a
connection failure; `cognee_statusline_render._url_mismatch` decides whether the
*renderer* trusts the resulting marker. They are mirror images and must stay
equivalent — if a hook records a state the renderer then ignores (or the reverse),
the user is left looking at a stale glyph. The duplication is forced: the renderer
is standalone by design and must not import `_plugin_common`, so nothing but a
test can hold the two together. This is that test.

It also pins the two invariants a review of this logic will reasonably question:

  * the permissive direction is deliberate — "same target" unless the URLs provably
    differ, so a server that genuinely died is still reported when a URL is unknown.
    Requiring both URLs would swallow exactly the transition the branch exists for;
  * `service_url` is never empty at the call site, because the runtime resolution
    falls back to a hard-coded localhost default. If that default is ever removed,
    the warming/died decision needs revisiting — this test fails loudly instead.

Migrated from {claude-code,codex}/tests/test_connection_target_match.py.
"""

from __future__ import annotations

import itertools

import pytest

_LOCAL = "http://localhost:8011"
_CLOUD = "https://example.cognee.ai"

# Every shape the two sides can see, including the ones a reviewer reaches for.
_URLS = ["", _LOCAL, _CLOUD, "http://127.0.0.1:8011"]


@pytest.fixture
def modules(suite, isolated_modules):
    pc = isolated_modules(suite, "_plugin_common")
    sl = isolated_modules(suite, "cognee_statusline_render")
    return pc, sl


# ── the two implementations agree, case by case ────────────────────────────


def test_the_two_predicates_are_equivalent(modules):
    """`same_connection_target(a, b) == (not _url_mismatch(a, b))`, exhaustively."""
    pc, sl = modules
    for a, b in itertools.product(_URLS, repeat=2):
        hook = pc.same_connection_target(a, b)
        renderer = not sl._url_mismatch(a, b)
        assert hook == renderer, f"drifted on ({a!r}, {b!r}): hook={hook} renderer={renderer}"


def test_equivalence_holds_for_the_reviewers_scenario(modules):
    """Cloud marker + a local session whose URL is somehow unknown."""
    pc, sl = modules
    assert pc.same_connection_target("", _CLOUD) is True
    assert sl._url_mismatch("", _CLOUD) is False


# ── the permissive direction, stated as behaviour ─────────────────────────


def test_provably_different_urls_are_not_the_same_target(modules):
    pc, _ = modules
    assert pc.same_connection_target(_LOCAL, _CLOUD) is False
    assert pc.same_connection_target(_LOCAL, "http://127.0.0.1:8011") is False


def test_identical_urls_are_the_same_target(modules):
    pc, _ = modules
    assert pc.same_connection_target(_LOCAL, _LOCAL) is True


def test_unknown_url_on_either_side_reads_as_the_same_target(modules):
    """Deliberate: a genuine "server died" must still be reported, not swallowed."""
    pc, _ = modules
    assert pc.same_connection_target("", _CLOUD) is True
    assert pc.same_connection_target(_CLOUD, "") is True
    assert pc.same_connection_target("", "") is True


def test_trailing_slash_and_whitespace_do_not_split_a_target(modules):
    pc, _ = modules
    assert pc.same_connection_target(f"{_LOCAL}/", _LOCAL) is True
    assert pc.same_connection_target(f"  {_LOCAL}  ", _LOCAL) is True


# ── the premise: the hook's service_url is never empty ────────────────────


def test_runtime_base_url_is_never_empty(modules):
    """A false red during local warm-up needs service_url == ""; resolution prevents it.

    The harness scrubs COGNEE_BASE_URL / COGNEE_LOCAL_API_URL from the env, so
    this runs against a genuinely unset environment.
    """
    pc, _ = modules
    resolved = pc.resolve_runtime_mode()
    assert resolved["base_url"], "empty base_url would make the warming heuristic ambiguous"
    # Asserted against the constant rather than a `url_source` tag, which only the
    # Claude Code copy of resolve_runtime_mode() reports.
    assert resolved["base_url"] == pc._DEFAULT_LOCAL_SERVICE_URL, resolved["base_url"]


def test_explicit_urls_are_honoured_over_the_default(modules, monkeypatch):
    pc, _ = modules
    monkeypatch.setenv("COGNEE_BASE_URL", _CLOUD)
    resolved = pc.resolve_runtime_mode()
    assert resolved["base_url"] == _CLOUD, resolved
