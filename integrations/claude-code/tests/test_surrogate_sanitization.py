"""Regression tests for lone-Unicode-surrogate sanitization in the capture hooks.

Claude Code transcripts / hook payloads can legitimately contain lone
surrogates (e.g. U+DC88 from binary tool output rendered into the
transcript): json.loads accepts "\\udc88" escapes and yields a str with an
unpaired surrogate. If such a string reaches the session cache unmodified,
GET /api/v1/sessions/{id} 500s with UnicodeEncodeError during response
encoding and the improve pipeline retries forever on the same character.

These tests pin the capture-side fix: every stored text field must survive
str.encode("utf-8") after passing through the hooks.

Run: python -m pytest integrations/claude-code/tests/test_surrogate_sanitization.py
"""

import asyncio
import importlib.util
import json
import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS))


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


store_session = _load("store_to_session_mod", "store-to-session.py")
store_prompt = _load("store_user_prompt_mod", "store-user-prompt.py")

# What json.loads produces for a hook payload carrying a lone surrogate.
SURROGATE_TEXT = json.loads('{"v": "binary \\udc88 output"}')["v"]


def test_payload_surrogate_reproduces_encode_error():
    # Sanity-check the failure mode this suite guards against.
    try:
        SURROGATE_TEXT.encode("utf-8")
    except UnicodeEncodeError:
        pass
    else:
        raise AssertionError("expected lone surrogate to be un-encodable")


def test_truncate_str_sanitizes_untruncated_text():
    # The common case: text under the cap must NOT be returned verbatim.
    out = store_session._truncate_str(SURROGATE_TEXT, 4000)
    out.encode("utf-8")  # must not raise
    assert "\udc88" not in out
    assert out == "binary ? output"


def test_truncate_str_sanitizes_truncated_text():
    long_text = SURROGATE_TEXT * 500
    out = store_session._truncate_str(long_text, 100)
    out.encode("utf-8")  # must not raise
    assert out.endswith("...")
    assert "\udc88" not in out


def test_truncate_str_sanitizes_non_string_values():
    out = store_session._truncate_str({"nested": SURROGATE_TEXT}, 4000)
    out.encode("utf-8")  # must not raise
    assert "\udc88" not in out


def test_truncate_str_none_and_plain_text_unchanged():
    assert store_session._truncate_str(None, 100) == ""
    assert store_session._truncate_str("plain ascii", 100) == "plain ascii"
    assert store_session._truncate_str("émoji ✨", 100) == "émoji ✨"


def test_infer_status_error_message_sanitized():
    payload = {"tool_response": {"is_error": True, "error": SURROGATE_TEXT}}
    status, err = store_session._infer_status(payload)
    assert status == "error"
    err.encode("utf-8")  # must not raise


def test_store_user_prompt_sanitizes_pending_prompt():
    # Drive _store with all module seams patched; the prompt handed to
    # remember_pending_prompt must be strictly valid UTF-8.
    captured = {}
    saved = {
        k: getattr(store_prompt, k)
        for k in (
            "_load_session",
            "load_config",
            "touch_activity",
            "_ensure_idle_watcher",
            "resolve_runtime_mode",
            "server_ready_hint",
            "remember_pending_prompt",
            "hook_log",
            "notify",
            "bump_save_counter",
        )
    }
    store_prompt._load_session = lambda: ("sess1", "ds", "u1")
    store_prompt.load_config = lambda: {}
    store_prompt.touch_activity = lambda: None
    store_prompt._ensure_idle_watcher = lambda *a, **kw: None
    store_prompt.resolve_runtime_mode = lambda: {"mode": "http", "base_url": "http://x"}
    store_prompt.server_ready_hint = lambda base_url: True
    store_prompt.remember_pending_prompt = lambda session_id, prompt, **kw: captured.update(
        {"prompt": prompt, "context": kw.get("context", "")}
    )
    store_prompt.hook_log = lambda *a, **kw: None
    store_prompt.notify = lambda *a, **kw: None
    store_prompt.bump_save_counter = lambda *a, **kw: None
    try:
        asyncio.run(store_prompt._store(SURROGATE_TEXT, {"turn_id": "t1"}))
    finally:
        for k, v in saved.items():
            setattr(store_prompt, k, v)

    captured["prompt"].encode("utf-8")  # must not raise
    assert "\udc88" not in captured["prompt"]
    assert captured["prompt"] == "binary ? output"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
