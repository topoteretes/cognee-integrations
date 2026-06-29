"""Guardrails for the structured hook event taxonomy.

Asserts the three sources of truth stay in sync:
  1. event literals emitted via ``hook_log(...)`` in ``scripts/``
  2. the ``KNOWN_HOOK_EVENTS`` registry in ``scripts/_events.py``
  3. the documented set in ``EVENTS.md``

Run: python integrations/claude-code/tests/test_events.py
"""

import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

from _events import HOOK_EVENTS_BY_NAMESPACE, KNOWN_HOOK_EVENTS  # noqa: E402

_SCRIPT_FILES = [
    "store-user-prompt.py",
    "session-context-lookup.py",
    "session-start.py",
    "store-to-session.py",
    "pre-compact.py",
    "sync-session-to-graph.py",
    "_plugin_common.py",
]

_HOOK_LOG_CALL = re.compile(r"hook_log\(\s*[\"']([^\"']+)[\"']")
# leading `namespace.action` cell of each EVENTS.md event-table row
_DOC_EVENT = re.compile(r"^\|\s*`([a-z]+\.[a-z0-9_]+)`\s*\|", re.MULTILINE)


def _events_emitted_in_scripts() -> set:
    found = set()
    for name in _SCRIPT_FILES:
        text = (_ROOT / "scripts" / name).read_text(encoding="utf-8")
        found |= set(_HOOK_LOG_CALL.findall(text))
    return found


def _events_documented() -> set:
    text = (_ROOT / "EVENTS.md").read_text(encoding="utf-8")
    return set(_DOC_EVENT.findall(text))


def test_emitted_events_match_registry():
    emitted = _events_emitted_in_scripts()
    assert emitted == set(KNOWN_HOOK_EVENTS), {
        "emitted_not_registered": sorted(emitted - set(KNOWN_HOOK_EVENTS)),
        "registered_not_emitted": sorted(set(KNOWN_HOOK_EVENTS) - emitted),
    }


def test_documented_events_match_registry():
    documented = _events_documented()
    assert documented == set(KNOWN_HOOK_EVENTS), {
        "documented_not_registered": sorted(documented - set(KNOWN_HOOK_EVENTS)),
        "registered_not_documented": sorted(set(KNOWN_HOOK_EVENTS) - documented),
    }


def test_every_event_is_namespaced():
    for event in KNOWN_HOOK_EVENTS:
        namespace, _, action = event.partition(".")
        assert namespace and action, f"event is not namespaced: {event!r}"
        assert event in HOOK_EVENTS_BY_NAMESPACE.get(namespace, ()), (
            f"event {event!r} is not grouped under namespace {namespace!r}"
        )


def test_namespace_grouping_has_no_duplicates():
    flat = [e for events in HOOK_EVENTS_BY_NAMESPACE.values() for e in events]
    assert len(flat) == len(set(flat)), "duplicate event in HOOK_EVENTS_BY_NAMESPACE"
    assert len(flat) == len(KNOWN_HOOK_EVENTS)


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
    sys.exit(1 if failures else 0)
