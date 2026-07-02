# Structured Hook Events

The Claude Code plugin emits structured events through
[`_plugin_common.hook_log`](scripts/_plugin_common.py) — one JSON line per
event to `~/.cognee-plugin/hook.log`. Every event name is namespaced
`<namespace>.<event>` so a telemetry emitter can consume and route them
uniformly (subscribe to `recall.*`, `bridge.*`, `session.*`, …).

The canonical set lives in [`scripts/event_names.py`](scripts/event_names.py)
as `KNOWN_EVENTS`; `EVENT_RENAMES` in that module is the migration map from
the historical flat names. `tests/test_event_names.py` asserts that the events
emitted in the scripts, the module, and this document all agree.

**146 events across 19 namespaces.**

## Namespaces

| Namespace | Events | Purpose |
|---|---|---|
| `activity.*` | 2 | Activity-log bookkeeping. |
| `agent.*` | 7 | Agent register/unregister lifecycle. |
| `bootstrap.*` | 10 | First-run bootstrap of the plugin runtime (spawn/wait/health). |
| `bridge.*` | 12 | Session->graph HTTP bridge: posting a session document and polling cognify. |
| `context.*` | 5 | Context lookup for injecting relevant memory. |
| `install.*` | 10 | Runtime provisioning: venv, uv, cognee package, data dir. |
| `io.*` | 3 | Low-level JSON read/write and payload parsing failures. |
| `mode.*` | 2 | Endpoint / operating-mode selection decisions. |
| `precompact.*` | 5 | PreCompact hook: anchoring memory before a context compaction. |
| `prompt.*` | 5 | UserPromptSubmit handling and the prompt watcher. |
| `recall.*` | 6 | Recalling prior memory/context for the current turn. |
| `runtime.*` | 3 | Runtime-state probes (connection, users/me) and user resolution. |
| `server.*` | 8 | Local cognee server lifecycle and the bootstrap lock/ready flags. |
| `session.*` | 9 | Session identity, resolution, and the session<->path map. |
| `statusline.*` | 3 | Status line configuration. |
| `store.*` | 12 | Capturing tool calls / prompts into session memory (incl. counters). |
| `sync.*` | 24 | Background sync of a finished session into the knowledge graph. |
| `trace.*` | 5 | Storing/falling back the reasoning trace for a turn. |
| `watcher.*` | 15 | Background watcher processes (idle, exit, prompt, generic). |

## Events by namespace

### `activity.*`

Activity-log bookkeeping.

| Event | Legacy name |
|---|---|
| `activity.log_write_failed` | `activity_log_write_failed` |
| `activity.touch_failed` | `activity_touch_failed` |

### `agent.*`

Agent register/unregister lifecycle.

| Event | Legacy name |
|---|---|
| `agent.lifecycle_error` | `agent_lifecycle_error` |
| `agent.register_failed` | `agent_register_failed` |
| `agent.register_result` | `agent_register_result` |
| `agent.unregister_failed` | `agent_unregister_failed` |
| `agent.unregister_result` | `agent_unregister_result` |
| `agent.unregister_skipped_no_auth` | `agent_unregister_skipped_no_auth` |
| `agent.unregister_skipped_no_session_name` | `agent_unregister_skipped_no_session_name` |

### `bootstrap.*`

First-run bootstrap of the plugin runtime (spawn/wait/health).

| Event | Legacy name |
|---|---|
| `bootstrap.complete` | `bootstrap_complete` |
| `bootstrap.failed` | `bootstrap_failed` |
| `bootstrap.lock_release_failed` | `bootstrap_lock_release_failed` |
| `bootstrap.lock_unlink_failed` | `bootstrap_lock_unlink_failed` |
| `bootstrap.log_open_failed` | `bootstrap_log_open_failed` |
| `bootstrap.main_exception` | `bootstrap_main_exception` |
| `bootstrap.server_unhealthy` | `bootstrap_server_unhealthy` |
| `bootstrap.spawn_failed` | `bootstrap_spawn_failed` |
| `bootstrap.spawned` | `bootstrap_spawned` |
| `bootstrap.waiting_for_peer` | `bootstrap_waiting_for_peer` |

### `bridge.*`

Session->graph HTTP bridge: posting a session document and polling cognify.

| Event | Legacy name |
|---|---|
| `bridge.auto_error` | `auto_bridge_error` |
| `bridge.auto_fired` | `auto_bridge_fired` |
| `bridge.cognify_poll_transient` | `cognify_poll_transient` |
| `bridge.deadline_exceeded` | `http_bridge_deadline_exceeded` |
| `bridge.done` | `http_bridge_done` |
| `bridge.failed` | `http_bridge_failed` |
| `bridge.no_dataset_id` | `http_bridge_no_dataset_id` |
| `bridge.parse_error` | `http_bridge_parse_error` |
| `bridge.poll` | `http_bridge_poll` |
| `bridge.post_failed` | `http_bridge_post_failed` |
| `bridge.skipped_empty_cache` | `http_bridge_skipped_empty_cache` |
| `bridge.skipped_no_api_key` | `http_bridge_skipped_no_api_key` |

### `context.*`

Context lookup for injecting relevant memory.

| Event | Legacy name |
|---|---|
| `context.lookup_empty` | `context_lookup_empty` |
| `context.lookup_exception` | `context_lookup_exception` |
| `context.lookup_hit` | `context_lookup_hit` |
| `context.lookup_missing_session_key` | `context_lookup_missing_session_key` |
| `context.lookup_session_key` | `context_lookup_session_key` |

### `install.*`

Runtime provisioning: venv, uv, cognee package, data dir.

| Event | Legacy name |
|---|---|
| `install.cognee_failed` | `cognee_install_failed` |
| `install.cognee_ready` | `cognee_install_ready` |
| `install.data_dir_mkdir_failed` | `cognee_data_dir_mkdir_failed` |
| `install.uv_failed` | `uv_install_failed` |
| `install.venv_lock_release_failed` | `venv_install_lock_release_failed` |
| `install.venv_lock_unlink_failed` | `venv_install_lock_unlink_failed` |
| `install.venv_ready_write_failed` | `venv_ready_write_failed` |
| `install.venv_reexec_failed` | `venv_reexec_failed` |
| `install.venv_unusable` | `cognee_venv_unusable` |
| `install.version_probe_failed` | `cognee_version_probe_failed` |

### `io.*`

Low-level JSON read/write and payload parsing failures.

| Event | Legacy name |
|---|---|
| `io.invalid_payload_json` | `invalid_payload_json` |
| `io.json_load_failed` | `json_load_failed` |
| `io.json_write_failed` | `json_write_failed` |

### `mode.*`

Endpoint / operating-mode selection decisions.

| Event | Legacy name |
|---|---|
| `mode.decision` | `mode_decision` |
| `mode.endpoint_selected` | `endpoint_mode_selected` |

### `precompact.*`

PreCompact hook: anchoring memory before a context compaction.

| Event | Legacy name |
|---|---|
| `precompact.anchor` | `precompact_anchor` |
| `precompact.direct_fetch_error` | `precompact_direct_fetch_error` |
| `precompact.empty` | `precompact_empty` |
| `precompact.recall_error` | `precompact_recall_error` |
| `precompact.run_exception` | `precompact_run_exception` |

### `prompt.*`

UserPromptSubmit handling and the prompt watcher.

| Event | Legacy name |
|---|---|
| `prompt.missing_session_key` | `prompt_missing_session_key` |
| `prompt.pending` | `prompt_pending` |
| `prompt.prepare_warning` | `prompt_prepare_warning` |
| `prompt.run_exception` | `prompt_run_exception` |
| `prompt.session_key` | `prompt_session_key` |

### `recall.*`

Recalling prior memory/context for the current turn.

| Event | Legacy name |
|---|---|
| `recall.audit_write_failed` | `recall_audit_write_failed` |
| `recall.breaker_open` | `recall_breaker_open` |
| `recall.budget_exceeded` | `recall_budget_exceeded` |
| `recall.error` | `recall_error` |
| `recall.last_write_failed` | `last_recall_write_failed` |
| `recall.skipped_warming` | `recall_skipped_warming` |

### `runtime.*`

Runtime-state probes (connection, users/me) and user resolution.

| Event | Legacy name |
|---|---|
| `runtime.resolve_user_failed` | `resolve_user_failed` |
| `runtime.state_connection_lookup_failed` | `runtime_state_connection_lookup_failed` |
| `runtime.state_users_me_failed` | `runtime_state_users_me_failed` |

### `server.*`

Local cognee server lifecycle and the bootstrap lock/ready flags.

| Event | Legacy name |
|---|---|
| `server.bootstrap_lock_acquired` | `server_bootstrap_lock_acquired` |
| `server.bootstrap_lock_release_failed` | `server_bootstrap_lock_release_failed` |
| `server.bootstrap_lock_released` | `server_bootstrap_lock_released` |
| `server.bootstrap_lock_stale_reaped` | `server_bootstrap_lock_stale_reaped` |
| `server.bootstrap_lock_unlink_failed` | `server_bootstrap_lock_unlink_failed` |
| `server.bootstrap_warning` | `server_bootstrap_warning` |
| `server.ready_clear_failed` | `server_ready_clear_failed` |
| `server.ready_mark_failed` | `server_ready_mark_failed` |

### `session.*`

Session identity, resolution, and the session<->path map.

| Event | Legacy name |
|---|---|
| `session.find_claude_parent_failed` | `find_claude_parent_failed` |
| `session.legacy_resolved_dir_remove_failed` | `legacy_resolved_dir_remove_failed` |
| `session.legacy_resolved_unlink_failed` | `legacy_resolved_unlink_failed` |
| `session.map_create_failed` | `map_create_failed` |
| `session.map_read_failed` | `session_map_read_failed` |
| `session.missing_payload_session_id` | `missing_payload_session_id` |
| `session.no_session_id` | `no_session_id` |
| `session.resolved` | `session_resolved` |
| `session.start_exception` | `session_start_exception` |

### `statusline.*`

Status line configuration.

| Event | Legacy name |
|---|---|
| `statusline.configured` | `statusline_configured` |
| `statusline.setup_failed` | `statusline_setup_failed` |
| `statusline.setup_skipped` | `statusline_setup_skipped` |

### `store.*`

Capturing tool calls / prompts into session memory (incl. counters).

| Event | Legacy name |
|---|---|
| `store.buffered_warming` | `store_buffered_warming` |
| `store.missing_session_key` | `store_missing_session_key` |
| `store.run_exception` | `run_exception` |
| `store.save_counter_read_failed` | `save_counter_read_failed` |
| `store.save_counter_reset_read_failed` | `save_counter_reset_read_failed` |
| `store.save_counter_reset_write_failed` | `save_counter_reset_write_failed` |
| `store.save_counter_write_failed` | `save_counter_write_failed` |
| `store.session_key` | `store_session_key` |
| `store.skip_self_cognee_bash` | `skip_self_cognee_bash` |
| `store.stop_error` | `stop_store_error` |
| `store.stop_stored` | `stop_stored` |
| `store.turn_counter_write_failed` | `turn_counter_write_failed` |

### `sync.*`

Background sync of a finished session into the knowledge graph.

| Event | Legacy name |
|---|---|
| `sync.bridge_done` | `sync_bridge_done` |
| `sync.deferred_to_shutdown_worker` | `sync_deferred_to_shutdown_worker` |
| `sync.detach_failed` | `sync_detach_failed` |
| `sync.detached_skipped_duplicate` | `sync_detached_skipped_duplicate` |
| `sync.failed` | `sync_failed` |
| `sync.final_already_claimed` | `final_sync_once_already_claimed` |
| `sync.final_claim_failed` | `final_sync_once_claim_failed` |
| `sync.final_claimed` | `final_sync_once_claimed` |
| `sync.final_no_token` | `final_sync_once_no_token` |
| `sync.final_prune_failed` | `final_sync_once_prune_failed` |
| `sync.final_pruned` | `final_sync_once_pruned` |
| `sync.lock_busy` | `sync_lock_busy` |
| `sync.lock_read_failed` | `sync_lock_read_failed` |
| `sync.lock_release_failed` | `sync_lock_release_failed` |
| `sync.lock_unlink_failed` | `sync_lock_unlink_failed` |
| `sync.missing_session_key` | `sync_missing_session_key` |
| `sync.no_target_sessions` | `sync_no_target_sessions` |
| `sync.payload` | `sync_payload` |
| `sync.retry_scheduled` | `sync_retry_scheduled` |
| `sync.session_key` | `sync_session_key` |
| `sync.skipped_lock_busy` | `sync_skipped_lock_busy` |
| `sync.start` | `sync_start` |
| `sync.start_delayed` | `sync_start_delayed` |
| `sync.stopped_watcher` | `sync_stopped_watcher` |

### `trace.*`

Storing/falling back the reasoning trace for a turn.

| Event | Legacy name |
|---|---|
| `trace.fallback_error` | `trace_fallback_error` |
| `trace.fallback_hit` | `trace_fallback_hit` |
| `trace.store_error` | `trace_store_error` |
| `trace.store_noresult` | `trace_store_noresult` |
| `trace.stored` | `trace_stored` |

### `watcher.*`

Background watcher processes (idle, exit, prompt, generic).

| Event | Legacy name |
|---|---|
| `watcher.exit_already_running` | `exit_watcher_already_running` |
| `watcher.exit_launch_failed` | `exit_watcher_launch_failed` |
| `watcher.exit_log_open_failed` | `exit_watcher_log_open_failed` |
| `watcher.exit_prune_failed` | `exit_watcher_prune_failed` |
| `watcher.exit_started` | `exit_watcher_started` |
| `watcher.idle_kill_failed` | `idle_watcher_kill_failed` |
| `watcher.idle_restart_failed` | `idle_watcher_restart_failed` |
| `watcher.idle_restarted` | `idle_watcher_restarted` |
| `watcher.log_open_failed` | `watcher_log_open_failed` |
| `watcher.prompt_alive_check_failed` | `prompt_watcher_alive_check_failed` |
| `watcher.prompt_log_open_failed` | `prompt_watcher_log_open_failed` |
| `watcher.prompt_stop_unlink_failed` | `prompt_watcher_stop_unlink_failed` |
| `watcher.sigterm_failed` | `watcher_sigterm_failed` |
| `watcher.stop_unlink_failed` | `watcher_stop_unlink_failed` |
| `watcher.stop_write_failed` | `watcher_stop_write_failed` |

## Adding a new event

1. Choose the right namespace (add one to `NAMESPACES` only if nothing fits).
2. Add the `"legacy_or_same": "namespace.event"` entry to `EVENT_RENAMES`
   in `scripts/event_names.py` (for a brand-new event, map it to itself).
3. Emit the namespaced name from your `hook_log(...)` call.
4. Add a row to the table above.
5. Run `pytest tests/test_event_names.py` — it fails if the three sets drift.

