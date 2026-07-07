"""Guard the structured-event taxonomy.

Asserts that the three sources of truth for hook event names stay in lockstep:

  1. the names actually emitted by ``hook_log(...)`` across ``scripts/``,
  2. the canonical set declared in ``scripts/event_names.py`` (``KNOWN_EVENTS``),
  3. the set documented in ``EVENTS.md``.

If any of them drifts (a new event added without updating the map or the doc,
a typo, an un-namespaced name), one of these tests fails with a precise diff.

Run: python integrations/claude-code/tests/test_event_names.py  (or via pytest).
"""

import pathlib
import re
import sys

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
DOC = pathlib.Path(__file__).resolve().parents[1] / "EVENTS.md"
sys.path.insert(0, str(SCRIPTS))

import event_names as en  # noqa: E402

# First string literal of every hook_log(...) call (same-line or multiline).
_HOOK_LOG = re.compile(r"hook_log\(\s*([\"'])([A-Za-z0-9_.]+)\1", re.DOTALL)
# `namespace.event` in a Markdown inline-code span, e.g. `recall.error`.
_DOC_EVENT = re.compile(r"`([a-z]+(?:\.[a-z0-9_]+)+)`")


def _emitted_events() -> set[str]:
    """Every event name passed to hook_log() across the plugin scripts."""
    events: set[str] = set()
    for path in SCRIPTS.glob("*.py"):
        if path.name == "event_names.py":
            continue
        for match in _HOOK_LOG.finditer(path.read_text()):
            events.add(match.group(2))
    return events


def _documented_events() -> set[str]:
    """Namespaced events referenced in the per-namespace tables of EVENTS.md."""
    return {
        token
        for token in _DOC_EVENT.findall(DOC.read_text())
        if token.split(".")[0] in en.NAMESPACES
    }


def test_every_event_is_namespaced():
    """No flat legacy names should remain; each is `namespace.event`."""
    bad = [e for e in en.KNOWN_EVENTS if not re.fullmatch(r"[a-z]+(?:\.[a-z0-9_]+)+", e)]
    assert not bad, f"malformed / un-namespaced event names: {sorted(bad)}"


def test_known_namespaces_are_declared():
    """Every namespace used by a known event is declared in NAMESPACES."""
    used = {e.split(".")[0] for e in en.KNOWN_EVENTS}
    assert used <= set(en.NAMESPACES), f"undeclared namespaces: {sorted(used - set(en.NAMESPACES))}"


def test_rename_map_targets_are_known_events():
    """Every value in the rename map is a canonical known event, and the set matches."""
    assert set(en.EVENT_RENAMES.values()) == set(en.KNOWN_EVENTS)


def test_emitted_events_match_known_events():
    """The events emitted in the scripts are exactly the documented known set."""
    emitted = _emitted_events()
    known = set(en.KNOWN_EVENTS)
    assert emitted == known, (
        f"emitted-but-unknown: {sorted(emitted - known)}; "
        f"known-but-unemitted: {sorted(known - emitted)}"
    )


def test_events_md_matches_known_events():
    """EVENTS.md documents exactly the canonical known set."""
    documented = _documented_events()
    known = set(en.KNOWN_EVENTS)
    assert documented == known, (
        f"documented-but-unknown: {sorted(documented - known)}; "
        f"known-but-undocumented: {sorted(known - documented)}"
    )


if __name__ == "__main__":
    test_every_event_is_namespaced()
    test_known_namespaces_are_declared()
    test_rename_map_targets_are_known_events()
    test_emitted_events_match_known_events()
    test_events_md_matches_known_events()
    print(
        f"OK: {len(en.KNOWN_EVENTS)} events across {len(en.NAMESPACES)} namespaces "
        "agree across scripts, event_names.py, and EVENTS.md."
    )
