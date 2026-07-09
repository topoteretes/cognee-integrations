import pathlib
import re


SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
EVENTS_MD = SCRIPT_DIR / "EVENTS.md"
PLUGIN_COMMON = SCRIPT_DIR / "_plugin_common.py"


def _documented_events() -> set[str]:
    lines = EVENTS_MD.read_text(encoding="utf-8").splitlines()
    return {line.strip()[2:-1] for line in lines if re.match(r"^- `.+`$", line.strip())}


def _mapped_events() -> set[str]:
    text = PLUGIN_COMMON.read_text(encoding="utf-8")
    start = text.index("_EVENT_NAME_MAP = {")
    end = text.index("}\n\n\ndef _normalize_event_name")
    block = text[start:end]
    events = set()
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        key = line.split('"', 2)[1]
        events.add(key)
    return {_normalize(key) for key in events}


def _normalize(event: str) -> str:
    return {
        "session_map_read_failed": "session.map_read_failed",
        "map_create_failed": "session.map_create_failed",
        "runtime_state_users_me_failed": "runtime.users_me_failed",
        "runtime_state_connection_lookup_failed": "runtime.connection_lookup_failed",
        "json_load_failed": "fs.json_load_failed",
        "json_write_failed": "fs.json_write_failed",
        "resolve_user_failed": "runtime.resolve_user_failed",
        "venv_reexec_failed": "process.venv_reexec_failed",
        "activity_log_write_failed": "fs.activity_log_write_failed",
        "save_counter_read_failed": "fs.save_counter_read_failed",
        "save_counter_write_failed": "fs.save_counter_write_failed",
        "save_counter_reset_read_failed": "fs.save_counter_reset_read_failed",
        "save_counter_reset_write_failed": "fs.save_counter_reset_write_failed",
        "turn_counter_write_failed": "fs.turn_counter_write_failed",
        "activity_touch_failed": "fs.activity_touch_failed",
        "sync_lock_read_failed": "session.sync_lock_read_failed",
        "sync_lock_unlink_failed": "session.sync_lock_unlink_failed",
        "sync_lock_busy": "session.sync_lock_busy",
        "sync_lock_release_failed": "session.sync_lock_release_failed",
        "server_ready_mark_failed": "runtime.server_ready_mark_failed",
        "server_ready_clear_failed": "runtime.server_ready_clear_failed",
        "find_claude_parent_failed": "host.find_parent_failed",
        "statusline_setup_skipped": "statusline.setup_skipped",
        "statusline_configured": "statusline.configured",
        "statusline_setup_failed": "statusline.setup_failed",
        "cognify_poll_failed": "cognify.poll_failed",
        "cognify_poll_transient": "cognify.poll_transient",
        "agent_register_failed": "agent.register_failed",
        "agent_unregister_failed": "agent.unregister_failed",
        "http_bridge_skipped_no_api_key": "bridge.skipped_no_api_key",
        "http_bridge_skipped_empty_cache": "bridge.skipped_empty_cache",
        "http_bridge_done": "bridge.done",
        "http_bridge_failed": "bridge.failed",
        "http_bridge_deadline_exceeded": "bridge.deadline_exceeded",
        "http_bridge_parse_error": "bridge.parse_error",
        "http_bridge_no_dataset_id": "bridge.no_dataset_id",
    }[event]


def test_documented_events_match_map():
    assert _documented_events() == _mapped_events()
