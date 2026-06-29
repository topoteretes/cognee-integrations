# Hook Event Reference

Every plugin hook script emits structured, single-line JSON events to
`~/.cognee-plugin/claude-code/hook.log` via `hook_log(event, detail)` (defined in
[`scripts/_plugin_common.py`](scripts/_plugin_common.py)). Each line has the shape:

```json
{"ts": "2026-06-30T12:00:00+00:00", "pid": 12345, "event": "recall.hit", "detail": {"chars": 812}}
```

## Taxonomy

Event names follow a `namespace.action` convention so a telemetry emitter can
consume them uniformly — filter or aggregate by namespace prefix (e.g. all
`recall.*` or all `bridge.*`). The canonical list lives in
[`scripts/_events.py`](scripts/_events.py) as `KNOWN_HOOK_EVENTS`, and
[`tests/test_events.py`](tests/test_events.py) asserts that the events emitted in
`scripts/`, the registry, and this document stay in sync.

There are **146 events** across **16 namespaces**:

| Namespace | Events | Purpose |
| --- | --- | --- |
| `agent.*` | 8 | Registering and unregistering the agent session with Cognee, plus user resolution. |
| `bootstrap.*` | 16 | Spawning and coordinating the local Cognee API server bootstrap, including its locks. |
| `bridge.*` | 11 | The HTTP bridge that flushes buffered Q&A and tool traces to the Cognee API and polls for completion. |
| `cognify.*` | 1 | Polling the Cognee `cognify` graph-build pipeline for status. |
| `install.*` | 10 | Bootstrapping the plugin runtime: uv, the venv, and the Cognee install. |
| `io.*` | 3 | Generic JSON serialization and incoming hook-payload parsing failures. |
| `precompact.*` | 5 | Building the pre-compaction memory anchor (`PreCompact`). |
| `prompt.*` | 5 | Capturing the user prompt (`UserPromptSubmit`) and buffering it for the bridge. |
| `recall.*` | 13 | Context recall on prompt submit: cache hits/misses, the circuit breaker, budget limits, and audit writes. |
| `server.*` | 2 | Marking and clearing the shared server-ready marker. |
| `session.*` | 11 | Resolving the host session id to a Cognee session, endpoint/mode selection, and legacy cleanup. |
| `state.*` | 9 | Runtime state files: save/turn counters, the activity timestamp, and connection state probes. |
| `statusline.*` | 3 | Automatic Claude Code status line configuration. |
| `store.*` | 10 | Capturing tool traces (`PostToolUse`) and the assistant stop (`Stop`) into session memory. |
| `sync.*` | 24 | Syncing buffered session memory into graph memory at session end, plus the single-flight claim and lock. |
| `watcher.*` | 15 | Lifecycle of the background prompt, idle, and exit watcher processes. |

## Events

### `agent.*`

Registering and unregistering the agent session with Cognee, plus user resolution.

| Event | Emitted when | Source |
| --- | --- | --- |
| `agent.lifecycle_error` | Lifecycle error. | `session-start` |
| `agent.register_failed` | Register failed. | `_plugin_common` |
| `agent.register_result` | Register result. | `session-start` |
| `agent.resolve_user_failed` | Resolve user failed. | `_plugin_common` |
| `agent.unregister_failed` | Unregister failed. | `_plugin_common` |
| `agent.unregister_result` | Unregister result. | `sync-session-to-graph` |
| `agent.unregister_skipped_no_auth` | Unregister skipped no auth. | `sync-session-to-graph` |
| `agent.unregister_skipped_no_session_name` | Unregister skipped no session name. | `sync-session-to-graph` |

### `bootstrap.*`

Spawning and coordinating the local Cognee API server bootstrap, including its locks.

| Event | Emitted when | Source |
| --- | --- | --- |
| `bootstrap.complete` | Complete. | `session-start` |
| `bootstrap.failed` | Failed. | `session-start` |
| `bootstrap.lock_release_failed` | Lock release failed. | `session-start` |
| `bootstrap.lock_unlink_failed` | Lock unlink failed. | `session-start` |
| `bootstrap.log_open_failed` | Log open failed. | `session-start` |
| `bootstrap.main_exception` | Main exception. | `session-start` |
| `bootstrap.server_lock_acquired` | Server lock acquired. | `session-start` |
| `bootstrap.server_lock_release_failed` | Server lock release failed. | `session-start` |
| `bootstrap.server_lock_released` | Server lock released. | `session-start` |
| `bootstrap.server_lock_stale_reaped` | Server lock stale reaped. | `session-start` |
| `bootstrap.server_lock_unlink_failed` | Server lock unlink failed. | `session-start` |
| `bootstrap.server_unhealthy` | Server unhealthy. | `session-start` |
| `bootstrap.server_warning` | Server warning. | `session-start` |
| `bootstrap.spawn_failed` | Spawn failed. | `session-start` |
| `bootstrap.spawned` | Spawned. | `session-start` |
| `bootstrap.waiting_for_peer` | Waiting for peer. | `session-start` |

### `bridge.*`

The HTTP bridge that flushes buffered Q&A and tool traces to the Cognee API and polls for completion.

| Event | Emitted when | Source |
| --- | --- | --- |
| `bridge.auto_error` | Auto error. | `store-to-session` |
| `bridge.auto_fired` | Auto fired. | `store-to-session` |
| `bridge.deadline_exceeded` | Deadline exceeded. | `_plugin_common` |
| `bridge.done` | Done. | `_plugin_common` |
| `bridge.failed` | Failed. | `_plugin_common` |
| `bridge.no_dataset_id` | No dataset id. | `_plugin_common` |
| `bridge.parse_error` | Parse error. | `_plugin_common` |
| `bridge.poll` | Poll. | `_plugin_common` |
| `bridge.post_failed` | Post failed. | `_plugin_common` |
| `bridge.skipped_empty_cache` | Skipped empty cache. | `_plugin_common` |
| `bridge.skipped_no_api_key` | Skipped no api key. | `_plugin_common` |

### `cognify.*`

Polling the Cognee `cognify` graph-build pipeline for status.

| Event | Emitted when | Source |
| --- | --- | --- |
| `cognify.poll_transient` | Poll transient. | `_plugin_common` |

### `install.*`

Bootstrapping the plugin runtime: uv, the venv, and the Cognee install.

| Event | Emitted when | Source |
| --- | --- | --- |
| `install.cognee_failed` | Cognee failed. | `session-start` |
| `install.cognee_ready` | Cognee ready. | `session-start` |
| `install.data_dir_mkdir_failed` | Data dir mkdir failed. | `session-start` |
| `install.uv_failed` | Uv failed. | `session-start` |
| `install.venv_lock_release_failed` | Venv lock release failed. | `session-start` |
| `install.venv_lock_unlink_failed` | Venv lock unlink failed. | `session-start` |
| `install.venv_ready_write_failed` | Venv ready write failed. | `session-start` |
| `install.venv_reexec_failed` | Venv reexec failed. | `_plugin_common` |
| `install.venv_unusable` | Venv unusable. | `session-start` |
| `install.version_probe_failed` | Version probe failed. | `session-start` |

### `io.*`

Generic JSON serialization and incoming hook-payload parsing failures.

| Event | Emitted when | Source |
| --- | --- | --- |
| `io.invalid_payload_json` | Invalid payload json. | `store-to-session`, `store-user-prompt` |
| `io.json_load_failed` | Json load failed. | `_plugin_common` |
| `io.json_write_failed` | Json write failed. | `_plugin_common` |

### `precompact.*`

Building the pre-compaction memory anchor (`PreCompact`).

| Event | Emitted when | Source |
| --- | --- | --- |
| `precompact.anchor` | Anchor. | `pre-compact` |
| `precompact.direct_fetch_error` | Direct fetch error. | `pre-compact` |
| `precompact.empty` | Empty. | `pre-compact` |
| `precompact.recall_error` | Recall error. | `pre-compact` |
| `precompact.run_exception` | Run exception. | `pre-compact` |

### `prompt.*`

Capturing the user prompt (`UserPromptSubmit`) and buffering it for the bridge.

| Event | Emitted when | Source |
| --- | --- | --- |
| `prompt.missing_session_key` | Missing session key. | `store-user-prompt` |
| `prompt.pending` | Pending. | `store-user-prompt` |
| `prompt.prepare_warning` | Prepare warning. | `store-user-prompt` |
| `prompt.run_exception` | Run exception. | `store-user-prompt` |
| `prompt.session_key` | Session key. | `store-user-prompt` |

### `recall.*`

Context recall on prompt submit: cache hits/misses, the circuit breaker, budget limits, and audit writes.

| Event | Emitted when | Source |
| --- | --- | --- |
| `recall.audit_write_failed` | Audit write failed. | `session-context-lookup` |
| `recall.breaker_open` | Breaker open. | `session-context-lookup` |
| `recall.budget_exceeded` | Budget exceeded. | `session-context-lookup` |
| `recall.empty` | Empty. | `session-context-lookup` |
| `recall.error` | Error. | `session-context-lookup` |
| `recall.exception` | Exception. | `session-context-lookup` |
| `recall.hit` | Hit. | `session-context-lookup` |
| `recall.last_write_failed` | Last write failed. | `session-context-lookup` |
| `recall.missing_session_key` | Missing session key. | `session-context-lookup` |
| `recall.session_key` | Session key. | `session-context-lookup` |
| `recall.skipped_warming` | Skipped warming. | `session-context-lookup` |
| `recall.trace_fallback_error` | Trace fallback error. | `session-context-lookup` |
| `recall.trace_fallback_hit` | Trace fallback hit. | `session-context-lookup` |

### `server.*`

Marking and clearing the shared server-ready marker.

| Event | Emitted when | Source |
| --- | --- | --- |
| `server.ready_clear_failed` | Ready clear failed. | `_plugin_common` |
| `server.ready_mark_failed` | Ready mark failed. | `_plugin_common` |

### `session.*`

Resolving the host session id to a Cognee session, endpoint/mode selection, and legacy cleanup.

| Event | Emitted when | Source |
| --- | --- | --- |
| `session.endpoint_mode_selected` | Endpoint mode selected. | `session-start` |
| `session.find_parent_failed` | Find parent failed. | `session-start` |
| `session.legacy_resolved_dir_remove_failed` | Legacy resolved dir remove failed. | `session-start` |
| `session.legacy_resolved_unlink_failed` | Legacy resolved unlink failed. | `session-start` |
| `session.map_create_failed` | Map create failed. | `_plugin_common` |
| `session.map_read_failed` | Map read failed. | `_plugin_common` |
| `session.missing_payload_id` | Missing payload id. | `session-start` |
| `session.mode_decision` | Mode decision. | `session-context-lookup`, `store-to-session`, `store-user-prompt` |
| `session.no_id` | No id. | `pre-compact`, `session-context-lookup`, `store-to-session`, `store-user-prompt` |
| `session.resolved` | Resolved. | `session-start` |
| `session.start_exception` | Start exception. | `session-start` |

### `state.*`

Runtime state files: save/turn counters, the activity timestamp, and connection state probes.

| Event | Emitted when | Source |
| --- | --- | --- |
| `state.activity_log_write_failed` | Activity log write failed. | `_plugin_common` |
| `state.activity_touch_failed` | Activity touch failed. | `_plugin_common` |
| `state.connection_lookup_failed` | Connection lookup failed. | `_plugin_common` |
| `state.save_counter_read_failed` | Save counter read failed. | `_plugin_common` |
| `state.save_counter_reset_read_failed` | Save counter reset read failed. | `_plugin_common` |
| `state.save_counter_reset_write_failed` | Save counter reset write failed. | `_plugin_common` |
| `state.save_counter_write_failed` | Save counter write failed. | `_plugin_common` |
| `state.turn_counter_write_failed` | Turn counter write failed. | `_plugin_common` |
| `state.users_me_failed` | Users me failed. | `_plugin_common` |

### `statusline.*`

Automatic Claude Code status line configuration.

| Event | Emitted when | Source |
| --- | --- | --- |
| `statusline.configured` | Configured. | `session-start` |
| `statusline.setup_failed` | Setup failed. | `session-start` |
| `statusline.setup_skipped` | Setup skipped. | `session-start` |

### `store.*`

Capturing tool traces (`PostToolUse`) and the assistant stop (`Stop`) into session memory.

| Event | Emitted when | Source |
| --- | --- | --- |
| `store.buffered_warming` | Buffered warming. | `store-to-session` |
| `store.missing_session_key` | Missing session key. | `store-to-session` |
| `store.run_exception` | Run exception. | `store-to-session` |
| `store.session_key` | Session key. | `store-to-session` |
| `store.skip_self_cognee_bash` | Skip self cognee bash. | `store-to-session` |
| `store.stop_error` | Stop error. | `store-to-session` |
| `store.stop_stored` | Stop stored. | `store-to-session` |
| `store.trace_error` | Trace error. | `store-to-session` |
| `store.trace_noresult` | Trace noresult. | `store-to-session` |
| `store.trace_stored` | Trace stored. | `store-to-session` |

### `sync.*`

Syncing buffered session memory into graph memory at session end, plus the single-flight claim and lock.

| Event | Emitted when | Source |
| --- | --- | --- |
| `sync.bridge_done` | Bridge done. | `sync-session-to-graph` |
| `sync.deferred_to_shutdown_worker` | Deferred to shutdown worker. | `sync-session-to-graph` |
| `sync.detach_failed` | Detach failed. | `sync-session-to-graph` |
| `sync.detached_skipped_duplicate` | Detached skipped duplicate. | `sync-session-to-graph` |
| `sync.failed` | Failed. | `sync-session-to-graph` |
| `sync.lock_busy` | Lock busy. | `_plugin_common` |
| `sync.lock_read_failed` | Lock read failed. | `_plugin_common` |
| `sync.lock_release_failed` | Lock release failed. | `_plugin_common` |
| `sync.lock_unlink_failed` | Lock unlink failed. | `_plugin_common` |
| `sync.missing_session_key` | Missing session key. | `sync-session-to-graph` |
| `sync.no_target_sessions` | No target sessions. | `sync-session-to-graph` |
| `sync.once_already_claimed` | Once already claimed. | `sync-session-to-graph` |
| `sync.once_claim_failed` | Once claim failed. | `sync-session-to-graph` |
| `sync.once_claimed` | Once claimed. | `sync-session-to-graph` |
| `sync.once_no_token` | Once no token. | `sync-session-to-graph` |
| `sync.once_prune_failed` | Once prune failed. | `sync-session-to-graph` |
| `sync.once_pruned` | Once pruned. | `sync-session-to-graph` |
| `sync.payload` | Payload. | `sync-session-to-graph` |
| `sync.retry_scheduled` | Retry scheduled. | `sync-session-to-graph` |
| `sync.session_key` | Session key. | `sync-session-to-graph` |
| `sync.skipped_lock_busy` | Skipped lock busy. | `sync-session-to-graph` |
| `sync.start` | Start. | `sync-session-to-graph` |
| `sync.start_delayed` | Start delayed. | `sync-session-to-graph` |
| `sync.stopped_watcher` | Stopped watcher. | `sync-session-to-graph` |

### `watcher.*`

Lifecycle of the background prompt, idle, and exit watcher processes.

| Event | Emitted when | Source |
| --- | --- | --- |
| `watcher.exit_already_running` | Exit already running. | `session-start` |
| `watcher.exit_launch_failed` | Exit launch failed. | `session-start` |
| `watcher.exit_log_open_failed` | Exit log open failed. | `session-start` |
| `watcher.exit_prune_failed` | Exit prune failed. | `session-start` |
| `watcher.exit_started` | Exit started. | `session-start` |
| `watcher.idle_kill_failed` | Idle kill failed. | `session-start` |
| `watcher.idle_restart_failed` | Idle restart failed. | `store-user-prompt` |
| `watcher.idle_restarted` | Idle restarted. | `store-user-prompt` |
| `watcher.log_open_failed` | Log open failed. | `session-start` |
| `watcher.prompt_alive_check_failed` | Prompt alive check failed. | `store-user-prompt` |
| `watcher.prompt_log_open_failed` | Prompt log open failed. | `store-user-prompt` |
| `watcher.prompt_stop_unlink_failed` | Prompt stop unlink failed. | `store-user-prompt` |
| `watcher.sigterm_failed` | Sigterm failed. | `sync-session-to-graph` |
| `watcher.stop_unlink_failed` | Stop unlink failed. | `session-start` |
| `watcher.stop_write_failed` | Stop write failed. | `sync-session-to-graph` |

## Adding or renaming an event

Event names are part of the observability contract. To change them:

1. Update the `hook_log(...)` call site(s) in `scripts/`.
2. Update `HOOK_EVENTS_BY_NAMESPACE` in [`scripts/_events.py`](scripts/_events.py).
3. Update the table above.

`tests/test_events.py` fails if these three drift apart.
