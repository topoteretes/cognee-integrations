"""Recall can search the part of a wrapped prompt a person wrote.

COGNEE_RECALL_STRIP_TAGS removes named tag blocks and COGNEE_RECALL_QUERY_PATTERN
extracts the query from what is left. Both unset, recall searches the prompt as
sent. Capture (store-user-prompt) is unaffected.
"""

import io
import json

import pytest

WAKE = (
    '<wake reason="mention" current-time="2026-10-08T22:57:50Z">\n'
    '  <project id="chan_1" type="project">\n'
    '    <message trigger="true" from="human" trust="principal" id="cmsg_1" '
    'sent-at="2026-10-08T22:57:50Z">why doesn&#39;t the deploy pick up the new env?</message>\n'
    "  </project>\n"
    "</wake>"
)
WAKE_PATTERN = r'<message\b[^>]*\btrigger="true"[^>]*>(.*?)</message>'


def _run(hook, monkeypatch, prompt, **env):
    for name in ("COGNEE_RECALL_STRIP_TAGS", "COGNEE_RECALL_QUERY_PATTERN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(hook, "resolve_session_key_from_payload", lambda payload: ("key", "test"))
    monkeypatch.setattr(hook, "set_session_key", lambda value: None)
    monkeypatch.setattr(hook, "get_session_key", lambda: "key")
    recalled = []

    async def fake_run(prompt, cwd):
        recalled.append(prompt)
        return None

    monkeypatch.setattr(hook, "_run", fake_run)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": prompt, "cwd": "."})))
    hook.main()
    return recalled


def test_unset_recalls_on_the_prompt_as_sent(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    assert _run(hook, monkeypatch, WAKE) == [WAKE]
    assert _run(hook, monkeypatch, "  plain prompt  ") == ["  plain prompt  "]


def test_pattern_extracts_and_unescapes_the_message(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    got = _run(hook, monkeypatch, WAKE, COGNEE_RECALL_QUERY_PATTERN=WAKE_PATTERN)
    assert got == ["why doesn't the deploy pick up the new env?"]


def test_unmatched_prompt_is_used_as_is(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    prompt = "what remains outstanding on the deploy?"
    assert _run(hook, monkeypatch, prompt, COGNEE_RECALL_QUERY_PATTERN=WAKE_PATTERN) == [prompt]


def test_every_match_and_group_is_joined(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    prompt = (
        '<relay><cited author="user">drop the old branch</cited>'
        "<note>Delete it and note its tip SHA.</note></relay>"
    )
    pattern = r"<cited\b[^>]*>(.*?)</cited>|<note>(.*?)</note>"
    got = _run(hook, monkeypatch, prompt, COGNEE_RECALL_QUERY_PATTERN=pattern)
    assert got == ["drop the old branch\n\nDelete it and note its tip SHA."]


def test_pattern_without_groups_uses_the_whole_match(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    got = _run(
        hook, monkeypatch, "ticket ABC-123 is blocked", COGNEE_RECALL_QUERY_PATTERN=r"[A-Z]+-\d+"
    )
    assert got == ["ABC-123"]


def test_strip_tags_removes_blocks_with_attributes(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    prompt = (
        "<system-reminder>\nA background task started.\n</system-reminder>\n\n"
        '<ide-selection path="a.py">x = 1</ide-selection>\nrename this variable everywhere'
    )
    got = _run(hook, monkeypatch, prompt, COGNEE_RECALL_STRIP_TAGS="system-reminder, ide-selection")
    assert got == ["rename this variable everywhere"]


def test_strip_runs_before_the_pattern(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    prompt = "<system-reminder>ignore me</system-reminder>\n" + WAKE
    env = {
        "COGNEE_RECALL_STRIP_TAGS": "system-reminder",
        "COGNEE_RECALL_QUERY_PATTERN": WAKE_PATTERN,
    }
    assert _run(hook, monkeypatch, prompt, **env) == ["why doesn't the deploy pick up the new env?"]


@pytest.mark.parametrize("pattern", ["(unclosed", "<message[^>]*>(\\s*)</message>"])
def test_invalid_pattern_or_empty_groups_search_the_text(suite, hook_module, monkeypatch, pattern):
    hook = hook_module(suite, "session-context-lookup.py")
    prompt = "<message>   </message> please check the logs"
    assert _run(hook, monkeypatch, prompt, COGNEE_RECALL_QUERY_PATTERN=pattern) == [prompt]


def test_invalid_pattern_still_searches_the_stripped_text(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    prompt = "<system-reminder>noise</system-reminder>\nplease check the logs"
    env = {
        "COGNEE_RECALL_STRIP_TAGS": "system-reminder",
        "COGNEE_RECALL_QUERY_PATTERN": "(unclosed",
    }
    assert _run(hook, monkeypatch, prompt, **env) == ["please check the logs"]


def test_a_prompt_of_only_stripped_blocks_skips_recall(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    prompt = "<system-reminder>\nA background task finished.\n</system-reminder>\n"
    assert _run(hook, monkeypatch, prompt, COGNEE_RECALL_STRIP_TAGS="system-reminder") == []


def test_the_default_floor_applies_to_the_extracted_query(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    monkeypatch.delenv("COGNEE_RECALL_MIN_PROMPT_CHARS", raising=False)
    short = WAKE.replace("why doesn&#39;t the deploy pick up the new env?", "ok")
    assert _run(hook, monkeypatch, short, COGNEE_RECALL_QUERY_PATTERN=WAKE_PATTERN) == []
    five = WAKE.replace("why doesn&#39;t the deploy pick up the new env?", "go on")
    assert _run(hook, monkeypatch, five, COGNEE_RECALL_QUERY_PATTERN=WAKE_PATTERN) == ["go on"]


def test_a_raised_floor_applies_to_the_extracted_query(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    short = WAKE.replace("why doesn&#39;t the deploy pick up the new env?", "ok")
    env = {"COGNEE_RECALL_QUERY_PATTERN": WAKE_PATTERN, "COGNEE_RECALL_MIN_PROMPT_CHARS": "20"}
    assert _run(hook, monkeypatch, short, **env) == []
