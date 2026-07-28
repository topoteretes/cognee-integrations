"""Tests for the "is this marker about my server?" predicate, which exists twice.

`_plugin_common.same_connection_target` decides whether a *hook* may record a
connection failure; `cognee_statusline_render._url_mismatch` decides whether the
*renderer* trusts the resulting marker. They are mirror images and must stay
equivalent — if a hook records a state the renderer then ignores (or the reverse),
the user is left looking at a stale glyph. The duplication is forced: the renderer is
standalone by design and must not import `_plugin_common`, so nothing but a test can
hold the two together. This is that test.

It also pins the two invariants a review of this logic will reasonably question:

  * the permissive direction is deliberate — "same target" unless the URLs provably
    differ, so a server that genuinely died is still reported when a URL is unknown.
    Requiring both URLs would swallow exactly the transition the branch exists for;
  * `service_url` is never empty at the call site, because the runtime resolution
    falls back to a hard-coded localhost default. If that default is ever removed,
    the warming/died decision needs revisiting — this test fails loudly instead.

Run: python integrations/claude-code/tests/test_connection_target_match.py
(or via pytest).
"""

import itertools
import os
import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _plugin_common as pc  # noqa: E402
import cognee_statusline_render as sl  # noqa: E402

_LOCAL = "http://localhost:8011"
_CLOUD = "https://example.cognee.ai"

# Every shape the two sides can see, including the ones a reviewer reaches for.
_URLS = ["", _LOCAL, _CLOUD, "http://127.0.0.1:8011"]


# ── the two implementations agree, case by case ────────────────────────────


def test_the_two_predicates_are_equivalent():
    """`same_connection_target(a, b) == (not _url_mismatch(a, b))`, exhaustively."""
    for a, b in itertools.product(_URLS, repeat=2):
        hook = pc.same_connection_target(a, b)
        renderer = not sl._url_mismatch(a, b)
        assert hook == renderer, f"drifted on ({a!r}, {b!r}): hook={hook} renderer={renderer}"


def test_equivalence_holds_for_the_reviewers_scenario():
    """Cloud marker + a local session whose URL is somehow unknown."""
    assert pc.same_connection_target("", _CLOUD) is True
    assert sl._url_mismatch("", _CLOUD) is False


# ── the permissive direction, stated as behaviour ─────────────────────────


def test_provably_different_urls_are_not_the_same_target():
    assert pc.same_connection_target(_LOCAL, _CLOUD) is False
    assert pc.same_connection_target(_LOCAL, "http://127.0.0.1:8011") is False


def test_identical_urls_are_the_same_target():
    assert pc.same_connection_target(_LOCAL, _LOCAL) is True


def test_unknown_url_on_either_side_reads_as_the_same_target():
    """Deliberate: a genuine "server died" must still be reported, not swallowed."""
    assert pc.same_connection_target("", _CLOUD) is True
    assert pc.same_connection_target(_CLOUD, "") is True
    assert pc.same_connection_target("", "") is True


def test_trailing_slash_and_whitespace_do_not_split_a_target():
    assert pc.same_connection_target(f"{_LOCAL}/", _LOCAL) is True
    assert pc.same_connection_target(f"  {_LOCAL}  ", _LOCAL) is True


# ── the premise: the hook's service_url is never empty ────────────────────


def test_runtime_base_url_is_never_empty():
    """A false red during local warm-up needs service_url == ""; resolution prevents it."""
    saved = {k: os.environ.get(k) for k in ("COGNEE_LOCAL_API_URL", "COGNEE_BASE_URL")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        resolved = pc.resolve_runtime_mode()
        assert resolved["base_url"], "empty base_url would make the warming heuristic ambiguous"
        # Asserted against the constant rather than a `url_source` tag, which only the
        # Claude Code copy of resolve_runtime_mode() reports.
        assert resolved["base_url"] == pc._DEFAULT_LOCAL_SERVICE_URL, resolved["base_url"]
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_explicit_urls_are_honoured_over_the_default():
    saved = {k: os.environ.get(k) for k in ("COGNEE_LOCAL_API_URL", "COGNEE_BASE_URL")}
    try:
        os.environ.pop("COGNEE_LOCAL_API_URL", None)
        os.environ["COGNEE_BASE_URL"] = _CLOUD
        resolved = pc.resolve_runtime_mode()
        assert resolved["base_url"] == _CLOUD, resolved
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {_name}: {exc}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
