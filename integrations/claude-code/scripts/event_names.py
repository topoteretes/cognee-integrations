"""Canonical structured-event taxonomy for the Claude Code plugin.

Single source of truth for every event name passed to
``_plugin_common.hook_log``. Names are namespaced ``<namespace>.<event>``
so a downstream telemetry emitter can group and route them uniformly
(e.g. everything under ``recall.*`` or ``bridge.*``).

``EVENT_RENAMES`` is the one migration map from the historical flat
snake_case names to the namespaced names. ``KNOWN_EVENTS`` is the frozen
set of canonical names and is what tests and emitters should import.
Keep this module, EVENTS.md, and the emitted call sites in lockstep — a
test enforces that they agree.
"""

from __future__ import annotations

# Namespaces, in taxonomy order.
NAMESPACES: frozenset[str] = frozenset(
    {
        "activity",
        "agent",
        "bootstrap",
        "bridge",
        "context",
        "install",
        "io",
        "mode",
        "precompact",
        "prompt",
        "recall",
        "runtime",
        "server",
        "session",
        "statusline",
        "store",
        "sync",
        "trace",
        "watcher",
    }
)

# Historical flat name -> canonical namespaced name. The one rename map.
EVENT_RENAMES: dict[str, str] = {
    # activity.*
    "activity_log_write_failed": "activity.log_write_failed",
    "activity_touch_failed": "activity.touch_failed",
    # agent.*
    "agent_lifecycle_error": "agent.lifecycle_error",
    "agent_register_failed": "agent.register_failed",
    "agent_register_result": "agent.register_result",
    "agent_unregister_failed": "agent.unregister_failed",
    "agent_unregister_result": "agent.unregister_result",
    "agent_unregister_skipped_no_auth": "agent.unregister_skipped_no_auth",
    "agent_unregister_skipped_no_session_name": "agent.unregister_skipped_no_session_name",
    # bootstrap.*
    "bootstrap_complete": "bootstrap.complete",
    "bootstrap_failed": "bootstrap.failed",
    "bootstrap_lock_release_failed": "bootstrap.lock_release_failed",
    "bootstrap_lock_unlink_failed": "bootstrap.lock_unlink_failed",
    "bootstrap_log_open_failed": "bootstrap.log_open_failed",
    "bootstrap_main_exception": "bootstrap.main_exception",
    "bootstrap_server_unhealthy": "bootstrap.server_unhealthy",
    "bootstrap_spawn_failed": "bootstrap.spawn_failed",
    "bootstrap_spawned": "bootstrap.spawned",
    "bootstrap_waiting_for_peer": "bootstrap.waiting_for_peer",
    # bridge.*
    "auto_bridge_error": "bridge.auto_error",
    "auto_bridge_fired": "bridge.auto_fired",
    "cognify_poll_transient": "bridge.cognify_poll_transient",
    "http_bridge_deadline_exceeded": "bridge.deadline_exceeded",
    "http_bridge_done": "bridge.done",
    "http_bridge_failed": "bridge.failed",
    "http_bridge_no_dataset_id": "bridge.no_dataset_id",
    "http_bridge_parse_error": "bridge.parse_error",
    "http_bridge_poll": "bridge.poll",
    "http_bridge_post_failed": "bridge.post_failed",
    "http_bridge_skipped_empty_cache": "bridge.skipped_empty_cache",
    "http_bridge_skipped_no_api_key": "bridge.skipped_no_api_key",
    # context.*
    "context_lookup_empty": "context.lookup_empty",
    "context_lookup_exception": "context.lookup_exception",
    "context_lookup_hit": "context.lookup_hit",
    "context_lookup_missing_session_key": "context.lookup_missing_session_key",
    "context_lookup_session_key": "context.lookup_session_key",
    # install.*
    "cognee_install_failed": "install.cognee_failed",
    "cognee_install_ready": "install.cognee_ready",
    "cognee_data_dir_mkdir_failed": "install.data_dir_mkdir_failed",
    "uv_install_failed": "install.uv_failed",
    "venv_install_lock_release_failed": "install.venv_lock_release_failed",
    "venv_install_lock_unlink_failed": "install.venv_lock_unlink_failed",
    "venv_ready_write_failed": "install.venv_ready_write_failed",
    "venv_reexec_failed": "install.venv_reexec_failed",
    "cognee_venv_unusable": "install.venv_unusable",
    "cognee_version_probe_failed": "install.version_probe_failed",
    # io.*
    "invalid_payload_json": "io.invalid_payload_json",
    "json_load_failed": "io.json_load_failed",
    "json_write_failed": "io.json_write_failed",
    # mode.*
    "mode_decision": "mode.decision",
    "endpoint_mode_selected": "mode.endpoint_selected",
    # precompact.*
    "precompact_anchor": "precompact.anchor",
    "precompact_direct_fetch_error": "precompact.direct_fetch_error",
    "precompact_empty": "precompact.empty",
    "precompact_recall_error": "precompact.recall_error",
    "precompact_run_exception": "precompact.run_exception",
    # prompt.*
    "prompt_missing_session_key": "prompt.missing_session_key",
    "prompt_pending": "prompt.pending",
    "prompt_prepare_warning": "prompt.prepare_warning",
    "prompt_run_exception": "prompt.run_exception",
    "prompt_session_key": "prompt.session_key",
    # recall.*
    "recall_audit_write_failed": "recall.audit_write_failed",
    "recall_breaker_open": "recall.breaker_open",
    "recall_budget_exceeded": "recall.budget_exceeded",
    "recall_error": "recall.error",
    "last_recall_write_failed": "recall.last_write_failed",
    "recall_skipped_warming": "recall.skipped_warming",
    # runtime.*
    "resolve_user_failed": "runtime.resolve_user_failed",
    "runtime_state_connection_lookup_failed": "runtime.state_connection_lookup_failed",
    "runtime_state_users_me_failed": "runtime.state_users_me_failed",
    # server.*
    "server_bootstrap_lock_acquired": "server.bootstrap_lock_acquired",
    "server_bootstrap_lock_release_failed": "server.bootstrap_lock_release_failed",
    "server_bootstrap_lock_released": "server.bootstrap_lock_released",
    "server_bootstrap_lock_stale_reaped": "server.bootstrap_lock_stale_reaped",
    "server_bootstrap_lock_unlink_failed": "server.bootstrap_lock_unlink_failed",
    "server_bootstrap_warning": "server.bootstrap_warning",
    "server_ready_clear_failed": "server.ready_clear_failed",
    "server_ready_mark_failed": "server.ready_mark_failed",
    # session.*
    "find_claude_parent_failed": "session.find_claude_parent_failed",
    "legacy_resolved_dir_remove_failed": "session.legacy_resolved_dir_remove_failed",
    "legacy_resolved_unlink_failed": "session.legacy_resolved_unlink_failed",
    "map_create_failed": "session.map_create_failed",
    "session_map_read_failed": "session.map_read_failed",
    "missing_payload_session_id": "session.missing_payload_session_id",
    "no_session_id": "session.no_session_id",
    "session_resolved": "session.resolved",
    "session_start_exception": "session.start_exception",
    # statusline.*
    "statusline_configured": "statusline.configured",
    "statusline_setup_failed": "statusline.setup_failed",
    "statusline_setup_skipped": "statusline.setup_skipped",
    # store.*
    "store_buffered_warming": "store.buffered_warming",
    "store_missing_session_key": "store.missing_session_key",
    "run_exception": "store.run_exception",
    "save_counter_read_failed": "store.save_counter_read_failed",
    "save_counter_reset_read_failed": "store.save_counter_reset_read_failed",
    "save_counter_reset_write_failed": "store.save_counter_reset_write_failed",
    "save_counter_write_failed": "store.save_counter_write_failed",
    "store_session_key": "store.session_key",
    "skip_self_cognee_bash": "store.skip_self_cognee_bash",
    "stop_store_error": "store.stop_error",
    "stop_stored": "store.stop_stored",
    "turn_counter_write_failed": "store.turn_counter_write_failed",
    # sync.*
    "sync_bridge_done": "sync.bridge_done",
    "sync_deferred_to_shutdown_worker": "sync.deferred_to_shutdown_worker",
    "sync_detach_failed": "sync.detach_failed",
    "sync_detached_skipped_duplicate": "sync.detached_skipped_duplicate",
    "sync_failed": "sync.failed",
    "final_sync_once_already_claimed": "sync.final_already_claimed",
    "final_sync_once_claim_failed": "sync.final_claim_failed",
    "final_sync_once_claimed": "sync.final_claimed",
    "final_sync_once_no_token": "sync.final_no_token",
    "final_sync_once_prune_failed": "sync.final_prune_failed",
    "final_sync_once_pruned": "sync.final_pruned",
    "sync_lock_busy": "sync.lock_busy",
    "sync_lock_read_failed": "sync.lock_read_failed",
    "sync_lock_release_failed": "sync.lock_release_failed",
    "sync_lock_unlink_failed": "sync.lock_unlink_failed",
    "sync_missing_session_key": "sync.missing_session_key",
    "sync_no_target_sessions": "sync.no_target_sessions",
    "sync_payload": "sync.payload",
    "sync_retry_scheduled": "sync.retry_scheduled",
    "sync_session_key": "sync.session_key",
    "sync_skipped_lock_busy": "sync.skipped_lock_busy",
    "sync_start": "sync.start",
    "sync_start_delayed": "sync.start_delayed",
    "sync_stopped_watcher": "sync.stopped_watcher",
    # trace.*
    "trace_fallback_error": "trace.fallback_error",
    "trace_fallback_hit": "trace.fallback_hit",
    "trace_store_error": "trace.store_error",
    "trace_store_noresult": "trace.store_noresult",
    "trace_stored": "trace.stored",
    # watcher.*
    "exit_watcher_already_running": "watcher.exit_already_running",
    "exit_watcher_launch_failed": "watcher.exit_launch_failed",
    "exit_watcher_log_open_failed": "watcher.exit_log_open_failed",
    "exit_watcher_prune_failed": "watcher.exit_prune_failed",
    "exit_watcher_started": "watcher.exit_started",
    "idle_watcher_kill_failed": "watcher.idle_kill_failed",
    "idle_watcher_restart_failed": "watcher.idle_restart_failed",
    "idle_watcher_restarted": "watcher.idle_restarted",
    "watcher_log_open_failed": "watcher.log_open_failed",
    "prompt_watcher_alive_check_failed": "watcher.prompt_alive_check_failed",
    "prompt_watcher_log_open_failed": "watcher.prompt_log_open_failed",
    "prompt_watcher_stop_unlink_failed": "watcher.prompt_stop_unlink_failed",
    "watcher_sigterm_failed": "watcher.sigterm_failed",
    "watcher_stop_unlink_failed": "watcher.stop_unlink_failed",
    "watcher_stop_write_failed": "watcher.stop_write_failed",
}

# The canonical set of namespaced event names emitted by the plugin.
KNOWN_EVENTS: frozenset[str] = frozenset(EVENT_RENAMES.values())


def canonical(event: str) -> str:
    """Return the canonical namespaced name for *event*.

    Accepts either a legacy flat name (translated via EVENT_RENAMES) or an
    already-namespaced name (returned unchanged). Unknown names are returned
    as-is so logging never fails on a not-yet-registered event.
    """
    if event in EVENT_RENAMES:
        return EVENT_RENAMES[event]
    return event
