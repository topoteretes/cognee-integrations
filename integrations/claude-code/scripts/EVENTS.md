# Hook event taxonomy

The hook logger normalizes structured event names before writing them to `hook.log`.

## Namespaced events

- `agent.register_failed`
- `agent.unregister_failed`
- `bridge.deadline_exceeded`
- `bridge.done`
- `bridge.failed`
- `bridge.no_dataset_id`
- `bridge.parse_error`
- `bridge.skipped_empty_cache`
- `bridge.skipped_no_api_key`
- `cognify.poll_failed`
- `cognify.poll_transient`
- `fs.activity_log_write_failed`
- `fs.activity_touch_failed`
- `fs.json_load_failed`
- `fs.json_write_failed`
- `fs.save_counter_read_failed`
- `fs.save_counter_reset_read_failed`
- `fs.save_counter_reset_write_failed`
- `fs.save_counter_write_failed`
- `fs.turn_counter_write_failed`
- `host.find_parent_failed`
- `process.venv_reexec_failed`
- `runtime.connection_lookup_failed`
- `runtime.resolve_user_failed`
- `runtime.server_ready_clear_failed`
- `runtime.server_ready_mark_failed`
- `runtime.users_me_failed`
- `session.map_create_failed`
- `session.map_read_failed`
- `session.sync_lock_busy`
- `session.sync_lock_read_failed`
- `session.sync_lock_release_failed`
- `session.sync_lock_unlink_failed`
- `statusline.configured`
- `statusline.setup_failed`
- `statusline.setup_skipped`

