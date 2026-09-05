"""Hermes MemoryProvider implementation backed by Cognee."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import code_graph, dataset_overrides, exit_watcher
from .backend import MemoryBackend, build_backend, default_backend, has_cognee
from .config import (
    DEFAULT_DATASET,
    DEFAULT_IDENTITY_EMAIL,
    DEFAULT_IDENTITY_PASSWORD,
    DEFAULT_LOCAL_PORT,
    DEFAULT_SERVER_BOOT_TIMEOUT,
    SHARED_PLUGIN_STATE_DIR,
    load_config,
    resolve_hermes_home,
    resolve_local_roots,
    str_to_bool,
    write_env_vars,
)
from .config import (
    save_config as save_plugin_config,
)
from .schemas import (
    CODE_SEARCH_SCHEMA,
    FORGET_SCHEMA,
    RECALL_SCHEMA,
    REMEMBER_SCHEMA,
    SWITCH_DATASET_SCHEMA,
)
from .server_bootstrap import ensure_local_server

try:
    from agent.memory_provider import MemoryProvider
except ImportError:  # pragma: no cover - lets package smoke tests run outside Hermes.

    class MemoryProvider:  # type: ignore[no-redef]
        @property
        def name(self) -> str:
            raise NotImplementedError


logger = logging.getLogger(__name__)

_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120


def _safe_session_component(value: str) -> str:
    # Sanitization kept consistent with the other integrations' session-id helpers
    # (claude-code/codex `_sanitize_session_key`, openclaw `sanitizeSessionKey`):
    # keep alphanumerics plus `-` `_` `.`, replace others with `_`, trim `._` ends,
    # cap length at 120.
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)
    return safe.strip("._")[:120] or "session"


def _format_turn(user_content: str, assistant_content: str) -> str:
    return f"User: {user_content}\nAssistant: {assistant_content}"


def _coerce_result_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            dumped = value.dict()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    return {"text": str(value)}


def _result_text(value: Any) -> str:
    data = _coerce_result_dict(value)
    for key in ("answer", "text", "content", "chunk_text", "summary"):
        found = data.get(key)
        if found:
            return str(found)
    return str(value)


def _recall_failure_advice(exc: Exception) -> str:
    """One actionable sentence appended to a timeout-shaped recall failure.

    The default GRAPH_COMPLETION search runs an LLM per query, so on a local
    model a timeout is usually the search strategy, not an outage — and the
    model reading this error can fix it on the retry. Covers the HTTP
    transport (CogneeUnreachable wraps urllib's "timed out") and the SDK
    bridge (concurrent.futures.TimeoutError, empty message on 3.10).
    """
    if not isinstance(exc, (TimeoutError, concurrent.futures.TimeoutError)):
        text = str(exc).lower()
        if "timed out" not in text and "timeout" not in text:
            return ""
    return (
        " The default GRAPH_COMPLETION search runs an LLM per query and can be "
        "slow on local models — retry with search_type='CHUNKS' (fast raw-text "
        "retrieval) or scope='session', or raise COGNEE_RECALL_TIMEOUT."
    )


class CogneeMemoryProvider(MemoryProvider):
    """Cognee V2/V1.1 knowledge graph memory for Hermes Agent."""

    def __init__(self, backend: Optional[MemoryBackend] = None) -> None:
        self._config: dict[str, Any] = {}
        # How we reach cognee. An explicitly injected transport always wins;
        # otherwise initialize() picks one from config (see backend.build_backend).
        self._injected_backend = backend
        self._backend: MemoryBackend = backend or default_backend()
        self._initialized = False
        self._remote_mode = False
        # Set once a detached worker has taken this session's improve-then-
        # unregister; from then on this process must not touch either.
        self._close_handed_off = False
        self._session_id = ""
        self._session_cognee_id = ""
        self._dataset = DEFAULT_DATASET
        self._top_k = 5
        self._auto_route = True
        self._improve_on_end = True
        self._writes_enabled = True
        self._hermes_home: str | None = None
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: Optional[threading.Thread] = None
        # Bumped on a reset session switch to invalidate prefetches still in
        # flight — see queue_prefetch / on_session_switch.
        self._prefetch_generation = 0
        self._sync_thread: Optional[threading.Thread] = None
        # The breaker is touched from the main thread and every worker thread
        # (prefetch, sync, memory-write); the lock keeps the failure counter's
        # read-modify-writes from losing updates under that concurrency.
        self._breaker_lock = threading.Lock()
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        # Set when a crash-safe exit watcher is armed (server modes only).
        self._watcher_state_path: Optional[Path] = None
        # Dataset switching: the configured default, the per-conversation switch
        # counter (feeds hermes_<id>__N cognee session ids), and sessions retired
        # by a forced switch whose bridge failed — re-submitted at session end so
        # the escape hatch defers the sync instead of dropping turns.
        self._default_dataset = DEFAULT_DATASET
        self._switch_counter = 0
        self._retired_sessions: list[tuple[str, str]] = []
        # Where Hermes was launched; gates the identifier-based code recall lane.
        self._cwd = ""
        # Per-session memory-hit totals for the visibility header (guarded by
        # _prefetch_lock: written by prefetch workers, read on consumption).
        self._turns_seen = 0
        self._turns_with_hits = 0
        self._hits_total = 0
        self._cross_hits_total = 0

    @property
    def name(self) -> str:
        return "cognee"

    def is_available(self) -> bool:
        # Must stay network-free: Hermes calls this for every provider during
        # discovery, before anything is started.
        if not has_cognee():
            return False
        cfg = load_config()
        return bool(cfg.get("service_url") or cfg.get("llm_api_key"))

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "service_url",
                "description": "Cognee service URL (blank for local-server mode)",
                "required": False,
                "env_var": "COGNEE_BASE_URL",
            },
            {
                "key": "api_key",
                "description": "Cognee service API key",
                "secret": True,
                "required": False,
                "env_var": "COGNEE_API_KEY",
            },
            {
                "key": "llm_api_key",
                "description": "LLM API key for local embedded Cognee",
                "secret": True,
                "required": False,
                "env_var": "LLM_API_KEY",
            },
            {
                "key": "llm_model",
                "description": "LLM model for local embedded Cognee",
                "required": False,
                "env_var": "LLM_MODEL",
            },
            {
                "key": "dataset",
                "description": "Default Cognee dataset (shared with the other cognee plugins)",
                "default": DEFAULT_DATASET,
                "env_var": "COGNEE_PLUGIN_DATASET",
            },
            {
                "key": "auto_route",
                "description": "Let Cognee choose the recall strategy",
                "default": "true",
                "choices": ["true", "false"],
            },
            {
                "key": "improve_on_end",
                "description": "Run Cognee improve() when a Hermes session ends",
                "default": "true",
                "choices": ["true", "false"],
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        non_secret = {
            key: value for key, value in values.items() if key not in {"api_key", "llm_api_key"}
        }
        if non_secret:
            save_plugin_config(non_secret, hermes_home)

    def post_setup(self, hermes_home: str, config: dict[str, Any]) -> None:
        """Hermes memory setup hook for a focused Cognee setup flow."""
        print("\nCognee memory setup")
        print("-" * 40)
        print("  Deployment:")
        print("    local  - embedded Cognee in the Hermes process")
        print("    remote - Cognee service or Cognee Cloud")

        current = load_config(hermes_home)
        default_mode = "remote" if current.get("service_url") else "local"
        mode = _prompt("Mode", default=default_mode).strip().lower()
        remote = mode in {"remote", "cloud", "service"}

        env_values: dict[str, str] = {}
        file_values: dict[str, Any] = {
            "dataset": _prompt("Dataset", default=str(current.get("dataset") or DEFAULT_DATASET)),
            "auto_route": _prompt_bool("Auto-route recall", current.get("auto_route", True)),
            "improve_on_end": _prompt_bool(
                "Improve graph on session end",
                current.get("improve_on_end", True),
            ),
        }

        if remote:
            service_url = _prompt(
                "Cognee service URL",
                default=str(current.get("service_url") or ""),
            )
            if service_url:
                file_values["service_url"] = service_url
                env_values["COGNEE_BASE_URL"] = service_url
                env_values["COGNEE_SERVICE_URL"] = ""  # clear deprecated alias
            api_key = _prompt_secret("Cognee API key", keep=bool(current.get("api_key")))
            if api_key:
                env_values["COGNEE_API_KEY"] = api_key
        else:
            file_values["service_url"] = ""
            env_values["COGNEE_BASE_URL"] = ""
            env_values["COGNEE_SERVICE_URL"] = ""  # clear deprecated alias
            llm_key = _prompt_secret("LLM API key", keep=bool(current.get("llm_api_key")))
            if llm_key:
                env_values["LLM_API_KEY"] = llm_key
            llm_model = _prompt("LLM model", default=str(current.get("llm_model") or ""))
            if llm_model:
                file_values["llm_model"] = llm_model
                env_values["LLM_MODEL"] = llm_model

        save_plugin_config(file_values, hermes_home)
        write_env_vars(Path(hermes_home) / ".env", env_values)

        if not isinstance(config.get("memory"), dict):
            config["memory"] = {}
        config["memory"]["provider"] = self.name

        try:
            from hermes_cli.config import save_config

            save_config(config)
        except Exception:
            pass

        print("\n  Memory provider: cognee")
        print("  Activation saved to config.yaml")
        print("  Provider config saved to cognee.json")
        if env_values:
            print("  Secrets saved to .env")
        print("\n  Start a new Hermes session to activate Cognee memory.\n")

    def initialize(self, session_id: str, **kwargs) -> None:
        self._hermes_home = kwargs.get("hermes_home")
        self._config = load_config(self._hermes_home)
        self._session_id = session_id
        self._default_dataset = str(self._config.get("dataset") or DEFAULT_DATASET)
        self._dataset = self._default_dataset
        self._top_k = int(self._config.get("top_k") or 5)
        self._auto_route = str_to_bool(self._config.get("auto_route"), True)
        self._improve_on_end = str_to_bool(self._config.get("improve_on_end"), True)
        self._writes_enabled = kwargs.get("agent_context", "primary") in {"", "primary", None}
        self._session_cognee_id = self._build_cognee_session_id(session_id, **kwargs)
        self._apply_dataset_override()
        try:
            self._cwd = os.getcwd()
        except OSError:
            self._cwd = ""

        # Now that config is loaded, choose the transport (unless one was injected).
        if self._injected_backend is None:
            self._backend = build_backend(self._config)

        self._backend.configure_models(
            llm_api_key=str(self._config.get("llm_api_key") or ""),
            llm_model=str(self._config.get("llm_model") or ""),
        )

        # Connection mode (see README "Modes"):
        #   remote        — service_url set: thin client to a managed/cloud cognee.
        #   local-server  — default: ensure a local cognee server (single DB owner)
        #                   and connect as a thin client. No in-process DB ops, so
        #                   no "database is locked" under Hermes' background threads.
        #   embedded      — opt-in (COGNEE_EMBEDDED=true): run cognee in-process.
        #                   Single-process / offline only; the local single-writer
        #                   DBs are NOT safe under concurrent / multi-process use.
        service_url = str(self._config.get("service_url") or "")
        embedded = str_to_bool(self._config.get("embedded"), False)

        # No silent fallbacks between modes: a failure in an explicitly chosen mode
        # is surfaced. Falling back to embedded would reintroduce the exact DB-lock
        # risk this PR removes; falling back from remote to local would mask config
        # errors and silently diverge data. Embedded is reachable only on purpose
        # (COGNEE_EMBEDDED=true).
        if service_url:
            try:
                api_key = str(self._config.get("api_key") or "")
                self._backend.connect(url=service_url, api_key=api_key, timeout=30)
                self._remote_mode = True
            except Exception as exc:
                raise RuntimeError(
                    f"COGNEE_BASE_URL is set to {service_url!r} but the connection failed. "
                    "Fix the URL/network/credentials, or unset it to use local mode."
                ) from exc
        elif embedded:
            self._configure_local_roots()
            self._remote_mode = False
        else:
            try:
                # Pin the roots the other cognee plugins pin (~/.cognee by default),
                # so the store is the same no matter which plugin spawned the server
                # on this port first.
                data_root, system_root = self._local_roots()
                local_url = ensure_local_server(
                    int(self._config.get("local_port") or DEFAULT_LOCAL_PORT),
                    data_root=data_root,
                    system_root=system_root,
                    boot_timeout=float(
                        self._config.get("server_boot_timeout", DEFAULT_SERVER_BOOT_TIMEOUT)
                    ),
                )
                self._backend.connect(url=local_url, api_key="", timeout=30)
                self._remote_mode = True
            except Exception as exc:
                raise RuntimeError(
                    "cognee local server failed to start, which is required for safe "
                    "concurrent DB access. Check for a port conflict on "
                    f"{self._config.get('local_port') or DEFAULT_LOCAL_PORT}, "
                    "missing dependencies "
                    "(uvicorn/cognee), or permissions. To run single-process in-process "
                    "instead (no concurrency safety), set COGNEE_EMBEDDED=true."
                ) from exc

        # Identity only matters in embedded mode (a local relational DB exists to
        # hold the user). In server/remote mode the server owns identity via the
        # api-key principal, and touching the local DB here would be meaningless.
        # The backend owns whatever principal it resolves; the provider never
        # handles it.
        if not self._remote_mode:
            try:
                self._backend.resolve_identity(
                    email=str(self._config.get("identity_email") or DEFAULT_IDENTITY_EMAIL),
                    password=str(
                        self._config.get("identity_password") or DEFAULT_IDENTITY_PASSWORD
                    ),
                    timeout=30,
                )
            except Exception as exc:
                logger.warning(
                    "Cognee identity initialization failed; using backend default user: %s", exc
                )

        self._ensure_dataset()
        self._arm_exit_watcher()
        self._initialized = True

    def _ensure_dataset(self) -> None:
        """Create-or-return the dataset up front, like the other plugins do.

        A fresh server or cloud tenant has no datasets, so a session that opens
        with a recall would otherwise hit a missing dataset. Best-effort: a write
        creates the dataset implicitly anyway, so failing here only costs the
        recall-first case — not the session.
        """
        try:
            self._backend.ensure_dataset(dataset=self._dataset, timeout=30)
        except Exception as exc:
            logger.warning("Cognee dataset ensure failed (continuing): %s", exc)

    def _arm_exit_watcher(self) -> None:
        """Crash insurance: a detached process that closes the session if we can't.

        Only meaningful when a server outlives this process (the transport says so
        via ``connection_info``); embedded mode has nothing to unregister from and
        nobody left to run an improve. Best-effort: a session must not fail to
        start because its insurance did.
        """
        try:
            info = self._backend.connection_info()
        except Exception:
            info = None
        if not info or not info.get("url"):
            return
        state_dir = SHARED_PLUGIN_STATE_DIR / "hermes"
        state_path = state_dir / "exit-watchers" / f"{os.getpid()}.json"
        try:
            exit_watcher.arm(
                state_path=state_path,
                log_path=state_dir / "exit-watcher.log",
                parent_pid=os.getpid(),
                url=str(info.get("url") or ""),
                api_key=str(info.get("api_key") or ""),
                agent_session_name=str(info.get("agent_session_name") or "hermes"),
                dataset=self._dataset,
                session_id=self._session_cognee_id,
                improve=bool(self._writes_enabled and self._improve_on_end),
                improve_timeout=self._timeout("improve_timeout", 300),
            )
            self._watcher_state_path = state_path
        except Exception as exc:
            logger.debug("could not arm the cognee exit watcher: %s", exc)

    def _is_usable(self) -> bool:
        """False when initialization never completed, so nothing may touch cognee.

        ``MemoryManager.initialize_all`` catches and logs whatever ``initialize()``
        raises, then carries on — Hermes starts either way and the tool schemas
        stay registered. Without this gate the provider would keep serving calls
        against an unconnected transport: for the SDK that means falling back to
        in-process cognee, which is exactly the single-writer DB risk local-server
        mode exists to avoid. Fail closed and say so instead.
        """
        return self._initialized

    def _unavailable_envelope(self) -> str:
        return json.dumps(
            {
                "error": (
                    "Cognee memory is unavailable: initialization did not complete. "
                    "Check the Hermes logs for the cause and `hermes cognee status` "
                    "for the current configuration."
                )
            }
        )

    def system_prompt_block(self) -> str:
        # Never advertise memory the model cannot actually use.
        if not self._is_usable():
            return ""
        mode = "remote" if self._remote_mode else "local"
        lines = [
            "# Cognee Memory",
            f"Active ({mode}). Dataset: {self._dataset}.",
            "Use cognee_recall for prior context, cognee_remember for durable facts, "
            "cognee_forget when the user asks to remove Cognee memory, "
            "cognee_switch_dataset to move this conversation to another dataset, and "
            "cognee_code_search for structural questions about an indexed repository.",
        ]
        # The memory steer — the counterpart of claude-code's COGNEE_PREFER_MEMORY
        # and openclaw's memorySteer: without it the agent reaches for the host's
        # native memory files by habit and the graph never hears about it.
        if str_to_bool(self._config.get("memory_steer"), True):
            steer = str(self._config.get("memory_steer_text") or "").strip() or (
                "Cognee is the preferred, authoritative long-term memory. Consult "
                "recalled Cognee context first, and store durable knowledge through "
                "the cognee tools rather than Hermes' built-in memory."
            )
            lines.append(steer)
        return "\n".join(lines)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._is_usable():
            return ""
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        if not result:
            return ""
        return f"## Cognee Memory\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not self._is_usable() or not query or self._is_breaker_open():
            return

        cognee_session_id = self._session_cognee_id_for(session_id)
        # Snapshot the generation this prefetch belongs to. A reset session
        # switch bumps it, so a recall that was already in flight for the
        # previous conversation discards its result instead of landing in the
        # fresh one. Joining the worker on switch is not enough: a recall slower
        # than any bounded join would still write after the clear.
        generation = self._prefetch_generation

        def _run() -> None:
            if str_to_bool(self._config.get("recall_session_layers"), True):
                self._run_layered_prefetch(query, cognee_session_id, generation)
                return
            try:
                results = self._recall(
                    query,
                    scope="auto",
                    search_type=None,
                    top_k=min(self._top_k, 5),
                    session_id=cognee_session_id,
                )
                lines = self._format_recall_lines(results, limit=5)
                rendered = "\n".join(lines)
                with self._prefetch_lock:
                    self._turns_seen += 1
                    if lines:
                        self._hits_total += len(lines)
                        self._turns_with_hits += 1
                        rendered = self._hit_header(len(lines), 0) + rendered
                        # Drop the result if a reset invalidated it mid-recall.
                        if generation == self._prefetch_generation:
                            self._prefetch_result = rendered
                # The backend answered, so this is a success either way.
                self._record_success()
            except Exception as exc:
                self._record_failure()
                logger.debug("Cognee prefetch failed: %s", exc)

        self._prefetch_thread = threading.Thread(
            target=_run,
            daemon=True,
            name="cognee-hermes-prefetch",
        )
        self._prefetch_thread.start()

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if not self._is_usable() or not self._writes_enabled or self._is_breaker_open():
            return

        cognee_session_id = self._session_cognee_id_for(session_id)
        content = _format_turn(user_content, assistant_content)

        def _sync() -> None:
            try:
                self._backend.remember_session(
                    text=content,
                    session_id=cognee_session_id,
                    dataset=self._dataset,
                    timeout=self._timeout("write_timeout", 120),
                )
                self._record_success()
            except Exception as exc:
                self._record_failure()
                logger.warning("Cognee session sync failed: %s", exc)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(
            target=_sync,
            daemon=True,
            name="cognee-hermes-sync",
        )
        self._sync_thread.start()

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        schemas = [RECALL_SCHEMA, REMEMBER_SCHEMA, FORGET_SCHEMA]
        # Config may not be loaded yet when Hermes collects schemas; the
        # defaults (on) match load_config's, so both paths agree.
        if str_to_bool(self._config.get("dataset_switch_tool"), True):
            schemas.append(SWITCH_DATASET_SCHEMA)
        if str_to_bool(self._config.get("code_search_tool"), True):
            schemas.append(CODE_SEARCH_SCHEMA)
        return schemas

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if not self._is_usable():
            return self._unavailable_envelope()
        if self._is_breaker_open():
            return json.dumps(
                {
                    "error": (
                        "Cognee is temporarily unavailable after repeated failures. "
                        "The provider will retry automatically after cooldown."
                    )
                }
            )
        if tool_name == "cognee_recall":
            return self._handle_recall(args)
        if tool_name == "cognee_remember":
            return self._handle_remember(args)
        if tool_name == "cognee_forget":
            return self._handle_forget(args)
        if tool_name == "cognee_switch_dataset":
            return self._handle_switch_dataset(args)
        if tool_name == "cognee_code_search":
            return self._handle_code_search(args)
        return json.dumps({"error": f"Unknown Cognee tool: {tool_name}"})

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=10.0)
        if (
            not self._is_usable()
            or not self._writes_enabled
            or not self._improve_on_end
            or self._is_breaker_open()
        ):
            return
        # Sessions retired by a forced dataset switch get their deferred bridge
        # before the current session's own close.
        self._bridge_retired_sessions()
        if self._hand_off_session_close():
            return
        self._improve_inline()

    def _hand_off_session_close(self) -> bool:
        """Give improve-then-unregister to a detached worker. True when it took them.

        Two requirements that cannot both hold in this process: the user's exit
        must not wait on a graph build, and the improve must finish *before* the
        agent unregisters — the local server runs with COGNEE_AGENT_MODE=true and
        its watchdog SIGTERMs the server within 60s of the agent count reaching
        zero, running pipelines or not. Outside this process both hold at once:
        the worker blocks on the improve, and its blocking costs nobody anything.
        This is how the claude-code, codex and openclaw plugins close a session.
        """
        if self._watcher_state_path is None:
            return False  # embedded mode, or arming failed — nobody to hand to
        if str(self._config.get("improve_background") or "").strip():
            return False  # an explicit override asked for the in-process path
        try:
            info = self._backend.connection_info() or {}
            handed = exit_watcher.finalize(
                self._watcher_state_path,
                api_key=str(info.get("api_key") or ""),
                session_id=self._session_cognee_id,
                dataset=self._dataset,
                improve=True,
                improve_timeout=self._timeout("improve_timeout", 300),
            )
        except Exception as exc:
            logger.debug("could not hand off the cognee session close: %s", exc)
            return False
        self._close_handed_off = handed
        return handed

    def _improve_inline(self) -> None:
        """Bridge the session here, because no detached worker will.

        Synchronous by default: this is either embedded mode, where the work runs
        in this process and dies with it, or a failed handoff, where shutdown() is
        about to unregister and take the server with it.
        ``COGNEE_IMPROVE_BACKGROUND=true`` opts out for a server nothing here can
        shut down.
        """
        background = str_to_bool(self._config.get("improve_background"), False)
        try:
            self._backend.improve(
                dataset=self._dataset,
                session_ids=[self._session_cognee_id],
                background=background,
                timeout=self._timeout("improve_timeout", 300),
            )
            self._record_success()
            # The bridge ran; a crash after this point should not improve again.
            # The watcher stays armed for the unregister half.
            if self._watcher_state_path is not None:
                exit_watcher.update(self._watcher_state_path, improve=False)
        except Exception as exc:
            self._record_failure()
            logger.warning("Cognee session-end improve failed: %s", exc)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        self._session_id = new_session_id
        # A different conversation means its own dataset override (or none) and
        # its own switch counter — never the previous conversation's.
        self._dataset = self._default_dataset
        self._switch_counter = 0
        self._session_cognee_id = self._build_cognee_session_id(new_session_id, **kwargs)
        self._apply_dataset_override()
        if self._watcher_state_path is not None and not self._close_handed_off:
            # Re-point the crash insurance at the new session (and re-enable the
            # improve half in case a previous session end disabled it). Skipped
            # after a handoff: that state file describes the session a detached
            # worker is still closing, and is its to delete.
            exit_watcher.update(
                self._watcher_state_path,
                session_id=self._session_cognee_id,
                dataset=self._dataset,
                improve=bool(self._writes_enabled and self._improve_on_end),
            )
        if reset:
            with self._prefetch_lock:
                self._prefetch_result = ""
                # Invalidate any recall still in flight for the old conversation.
                # Only on reset: /resume, /branch and compression continue the same
                # logical conversation, so a prefetch issued for it stays valid.
                self._prefetch_generation += 1
                # The hit totals describe one conversation; a fresh one starts at 0.
                self._turns_seen = 0
                self._turns_with_hits = 0
                self._hits_total = 0
                self._cross_hits_total = 0

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        # ``_writes_enabled`` is checked here for the same reason sync_turn checks
        # it: a non-primary agent_context (a subagent) must not write to memory.
        # Without it a subagent's built-in memory write still reached the graph
        # while its conversation turns were correctly suppressed.
        if (
            not self._is_usable()
            or not self._writes_enabled
            or action not in {"add", "replace"}
            or not content
            or self._is_breaker_open()
        ):
            return
        metadata = dict(metadata or {})
        source = metadata.get("write_origin") or "hermes_memory_tool"
        payload = f"Hermes {target} memory ({action}, {source}): {content}"

        def _sync() -> None:
            try:
                self._remember_permanent(payload, self._dataset)
                self._record_success()
            except Exception as exc:
                self._record_failure()
                logger.debug("Cognee memory-write mirror failed: %s", exc)

        threading.Thread(target=_sync, daemon=True, name="cognee-hermes-memory-write").start()

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs,
    ) -> None:
        if not task and not result:
            return
        content = f"Delegated task: {task}\nResult: {result}"
        self.sync_turn(content, "", session_id=self._session_id)

    def shutdown(self) -> None:
        for thread in (self._prefetch_thread, self._sync_thread):
            if thread and thread.is_alive():
                thread.join(timeout=5.0)
        if self._close_handed_off:
            # A detached worker owns improve-then-unregister for this session.
            # Unregistering here would drop the agent count to zero while that
            # improve is still running, and COGNEE_AGENT_MODE's watchdog would
            # retire the server mid-promotion — the exact race the handoff
            # removes. The worker owns the state file too, so nothing to disarm.
            self._backend.close(timeout=5, unregister=False)
            return
        if self._watcher_state_path is not None:
            # No handoff, so this is the orderly path — stand the insurance down
            # BEFORE closing the backend, so the watcher can never race the
            # unregister that is about to happen.
            exit_watcher.disarm(self._watcher_state_path)
            self._watcher_state_path = None
        self._backend.close(timeout=5)

    def _build_cognee_session_id(self, session_id: str, **kwargs) -> str:
        # Convention across integrations: "{agent}_{native_session_id}" —
        # e.g. hermes_<hermes-session-id>. (kwargs like agent_workspace/user_id are
        # accepted for call-site compatibility but no longer embedded in the name.)
        prefix = str(self._config.get("session_prefix") or "hermes")
        return f"{prefix}_{_safe_session_component(session_id)}"

    def _session_cognee_id_for(self, session_id: str) -> str:
        if not session_id or session_id == self._session_id:
            return self._session_cognee_id
        return self._build_cognee_session_id(session_id)

    def _local_roots(self) -> tuple[str, str]:
        return resolve_local_roots(self._config)

    def _configure_local_roots(self) -> None:
        """Point the backend at local storage (in-process transports only)."""
        data_root, system_root = self._local_roots()
        self._backend.configure_local_roots(data_root=data_root, system_root=system_root)

    def backup_paths(self) -> list[str]:
        """Cognee storage that ``hermes backup`` would otherwise miss.

        ``hermes backup`` walks ``HERMES_HOME`` on its own, so only roots living
        elsewhere need declaring. By default that is all of them: local storage
        sits in the shared ``~/.cognee`` (see ``resolve_local_roots``), so a
        backup deliberately includes the machine's shared memory store. Must work
        without ``initialize()`` and without network, so it reads config directly.
        """
        config = load_config()
        home = resolve_hermes_home()
        external: list[str] = []
        for root in resolve_local_roots(config):
            path = Path(root).expanduser()
            if home is not None and path.is_relative_to(home):
                continue
            external.append(str(path))
        return external

    def _timeout(self, key: str, default: float) -> float:
        """A named, bounded timeout read from config at call time."""
        return float(self._config.get(key, default))

    def _recall_scope_params(
        self, scope: str, search_type: Any, session_id: str
    ) -> tuple[Optional[str], Optional[list[str]], Optional[str], str]:
        """Map the tool's ``scope`` onto the backend's explicit targets.

        ``session`` searches only this conversation's cache, ``graph`` only the
        permanent dataset, ``auto`` both. A ``search_type`` override is
        meaningless for a pure session lookup, so it is dropped there.

        The normalized scope name is returned alongside the targets so a
        transport can pass the decision on rather than re-derive it. An
        unrecognized name resolves to ``auto`` here rather than travelling
        onward, so the backend is never handed a scope the server would reject.
        """
        normalized = (scope or "auto").lower()
        if normalized == "session":
            if str_to_bool(self._config.get("recall_session_layers"), True):
                # The session corpus is three server scopes, not one: cached Q&A
                # turns, tool-call trace lessons, and distilled agent guidance.
                # Same scope list openclaw's memory_search corpus=sessions sends.
                return session_id, [self._dataset], None, ["session", "trace", "session_context"]
            return session_id, None, None, normalized
        query_type = search_type or None
        if normalized == "graph":
            return None, [self._dataset], query_type, normalized
        return session_id, [self._dataset], query_type, "auto"

    def _recall(
        self,
        query: str,
        *,
        scope: str,
        search_type: Any,
        top_k: int,
        session_id: str,
    ) -> list[Any]:
        target_session, datasets, query_type, resolved_scope = self._recall_scope_params(
            scope, search_type, session_id
        )
        return self._backend.recall(
            query=query,
            session_id=target_session,
            datasets=datasets,
            top_k=top_k,
            auto_route=self._auto_route,
            query_type=query_type,
            scope=resolved_scope,
            timeout=self._timeout("recall_timeout", 120),
        )

    def _remember_permanent(self, content: str, dataset: str) -> Any:
        return self._backend.remember_permanent(
            text=content,
            dataset=dataset,
            session_ids=[self._session_cognee_id],
            timeout=self._timeout("write_timeout", 120),
        )

    # -- layered per-prompt recall -------------------------------------------

    def _hit_header(self, hits: int, cross: int) -> str:
        """One plain-words line on what memory just contributed, or "".

        Must be called under ``_prefetch_lock`` after the counters were updated:
        the per-session totals it renders are the ones this turn just advanced.
        """
        if not str_to_bool(self._config.get("memory_hits"), True):
            return ""
        line = f"{hits} memory hit{'s' if hits != 1 else ''} this turn"
        if cross:
            line += f" ({cross} beyond this session)"
        line += f" · {self._turns_with_hits}/{self._turns_seen} turns had hits this session"
        return line + "\n"

    def _code_lane(self, query: str) -> dict[str, Any]:
        """The additive code-graph lane for this prompt, or {}.

        Syntactically gated: fires only when the prompt names an
        identifier-shaped token AND either the cwd sits inside a repo indexed
        via ``hermes cognee index-repo`` or ``code_datasets`` names one.
        """
        if not str_to_bool(self._config.get("code_graph_recall"), True):
            return {}
        try:
            lane = code_graph.auto_code_lane(query, self._cwd)
        except Exception as exc:
            logger.debug("code lane gate failed: %s", exc)
            lane = {}
        if lane:
            return lane
        extra = [
            name.strip()
            for name in str(self._config.get("code_datasets") or "").split(",")
            if name.strip()
        ]
        if not extra:
            return {}
        identifiers = code_graph.extract_identifiers(query)
        if not identifiers:
            return {}
        return {
            "dataset": extra[0],
            "identifier": identifiers[0],
            "code_query": code_graph.build_code_query(identifiers[0]),
        }

    @staticmethod
    def _is_graph_not_built(exc: Exception, scope: Any) -> bool:
        """A 404 on the graph scope means nobody has cognified the dataset yet —
        routine on a fresh install, so it must not read as a recall failure."""
        return scope == ["graph"] and getattr(exc, "status", None) == 404

    def _run_layered_prefetch(self, query: str, session_id: str, generation: int) -> None:
        """Fan recall out over the memory layers, cheap scopes first.

        With ``dataset_ids`` + ``search_type`` in a single request the server's
        ``auto`` scope resolves graph-only, so cached Q&A turns, trace lessons
        and distilled agent guidance never reached the prompt. One bounded call
        per scope instead, each rendered as its own labelled block; a failure in
        one lane never discards the others. The graph lane runs last — it is the
        only call that can consume a full per-call timeout, and running it
        earlier starves the cheap lanes out of the budget.
        """
        budget = self._config.get("recall_budget")
        deadline = time.monotonic() + (20.0 if budget is None else float(budget))
        recall_timeout = self._timeout("recall_timeout", 120)
        top_k = min(self._top_k, 5)

        lanes: list[tuple[str, dict[str, Any], bool]] = [
            ("session_memory", {"scope": ["session"]}, False),
            ("trace_lessons", {"scope": ["trace"]}, False),
            ("agent_guidance", {"scope": ["session_context"], "context_profile": "agent"}, True),
        ]
        code_lane = self._code_lane(query)
        if code_lane:
            lanes.append(
                (
                    "code_graph",
                    {
                        "scope": ["code"],
                        "datasets": [code_lane["dataset"]],
                        "code_query": code_lane["code_query"],
                    },
                    True,
                )
            )
        # HYBRID_COMPLETION combines BM25 + vector + graph retrieval; with
        # only_context the LLM completion is skipped server-side either way.
        lanes.append(
            ("graph_memory", {"scope": ["graph"], "query_type": "HYBRID_COMPLETION"}, True)
        )

        blocks: list[str] = []
        hits = 0
        cross = 0
        answered = False
        hard_failures = 0
        for label, spec, is_cross in lanes:
            remaining = deadline - time.monotonic()
            if remaining < 0.2:
                # Not enough budget left for an honest attempt; skipping beats
                # firing a request with a doomed deadline.
                break
            try:
                results = self._backend.recall(
                    query=query,
                    session_id=session_id,
                    datasets=spec.get("datasets") or [self._dataset],
                    top_k=top_k,
                    auto_route=True,
                    query_type=spec.get("query_type"),
                    scope=spec["scope"],
                    context_profile=spec.get("context_profile"),
                    code_query=spec.get("code_query"),
                    only_context=True,
                    timeout=min(recall_timeout, remaining),
                )
                answered = True
            except Exception as exc:
                if self._is_graph_not_built(exc, spec["scope"]):
                    answered = True
                    continue
                hard_failures += 1
                logger.debug("Cognee recall lane %s failed: %s", label, exc)
                continue
            lines = self._format_recall_lines(results, limit=top_k)
            if not lines:
                continue
            hits += len(lines)
            if is_cross:
                cross += len(lines)
            blocks.append(f"<{label}>\n" + "\n".join(lines) + f"\n</{label}>")

        # One verdict per turn, not per lane: a single dead server must not
        # feed the breaker five failures every prompt, and one healthy lane is
        # proof the backend is up.
        if answered:
            self._record_success()
        elif hard_failures:
            self._record_failure()

        with self._prefetch_lock:
            self._turns_seen += 1
            if blocks:
                self._turns_with_hits += 1
                self._hits_total += hits
                self._cross_hits_total += cross
                if generation == self._prefetch_generation:
                    self._prefetch_result = self._hit_header(hits, cross) + "\n\n".join(blocks)

    def _handle_recall(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return json.dumps({"error": "Missing required parameter: query"})
        top_k = min(max(1, int(args.get("top_k") or self._top_k)), 20)
        scope = str(args.get("scope") or "auto")
        search_type = args.get("search_type")

        try:
            results = self._recall(
                query,
                scope=scope,
                search_type=search_type,
                top_k=top_k,
                session_id=self._session_cognee_id,
            )
            self._record_success()
            items = [self._normalize_recall_item(item) for item in results]
            overflow = self._backend.overflow_hint()
            if not items:
                # Distinguish a genuine miss from a backend condition that makes
                # recall structurally unable to match (e.g. an embedder change
                # leaving stored and query vectors different sizes). A confirmed
                # cause is a hard error rather than a silent empty result.
                dim_message = self._backend.empty_recall_hint()
                if dim_message:
                    return json.dumps({"error": dim_message, "count": 0})
                if overflow:
                    return json.dumps({"error": overflow, "count": 0})
                return json.dumps({"result": "No relevant Cognee memory found.", "count": 0})
            envelope: dict[str, Any] = {"results": items, "count": len(items)}
            if overflow:
                # Non-empty results still warn: mean-pooled vectors match
                # *something*, so plausible-but-wrong hits are the overflow's
                # most misleading symptom.
                envelope["warning"] = overflow
            return json.dumps(envelope)
        except Exception as exc:
            self._record_failure()
            return json.dumps(
                {"error": f"Cognee recall failed: {exc}{_recall_failure_advice(exc)}"}
            )

    def _handle_remember(self, args: dict[str, Any]) -> str:
        content = str(args.get("content") or "").strip()
        if not content:
            return json.dumps({"error": "Missing required parameter: content"})
        dataset = str(args.get("dataset") or self._dataset)

        try:
            result = self._remember_permanent(content, dataset)
            self._record_success()
            status = getattr(result, "status", "completed")
            envelope = {"result": "Content stored in Cognee.", "status": str(status)}
            # Embedding runs server-side after this write returns, so a hint here
            # usually reports the *previous* write's overflow — still worth
            # surfacing, since the index is already degrading either way.
            overflow = self._backend.overflow_hint()
            if overflow:
                envelope["warning"] = overflow
            return json.dumps(envelope)
        except Exception as exc:
            self._record_failure()
            return json.dumps({"error": f"Cognee remember failed: {exc}"})

    _FORGET_MAX_SCANNED = 100
    _FORGET_MAX_CANDIDATES = 8
    _FORGET_PREVIEW_CHARS = 240

    def _resolve_dataset_id(self, name: str, *, timeout: float = 30.0) -> str:
        """The UUID of the named dataset, or "" when this principal cannot see it."""
        for row in self._backend.list_datasets(timeout=timeout):
            if str(row.get("name") or "") == name:
                return str(row.get("id") or "")
        return ""

    @staticmethod
    def _forget_terms(raw_terms: str) -> list[str]:
        return [token for token in raw_terms.lower().replace(",", " ").split() if len(token) >= 2]

    def _handle_forget(self, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").strip().lower()
        if action == "find":
            return self._forget_find(args)
        if action == "forget":
            return self._forget_delete(args)
        return json.dumps(
            {
                "error": (
                    "Specify action='find' (list candidate documents by terms) or "
                    "action='forget' (delete confirmed data_ids)."
                )
            }
        )

    def _forget_find(self, args: dict[str, Any]) -> str:
        terms = self._forget_terms(str(args.get("terms") or ""))
        if not terms:
            return json.dumps({"error": "action='find' requires terms describing the content."})
        dataset = str(args.get("dataset") or self._dataset)
        # Flush the turn currently being written so it is at least in the cache;
        # note that unbridged session turns are not documents yet either way.
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        try:
            dataset_id = self._resolve_dataset_id(dataset)
            if not dataset_id:
                return json.dumps({"error": f"Dataset {dataset!r} was not found."})
            items = self._backend.list_dataset_data(dataset_id=dataset_id, timeout=30.0)
            candidates: list[dict[str, Any]] = []
            scanned = 0
            # The doc cap alone is not a time bound — 100 raw reads at the
            # per-read timeout could hold the tool call for minutes. One overall
            # deadline; a partial scan is reported as such via `scanned`.
            scan_deadline = time.monotonic() + self._timeout("write_timeout", 120)
            for item in items[: self._FORGET_MAX_SCANNED]:
                data_id = str(item.get("id") or "")
                if not data_id:
                    continue
                if time.monotonic() >= scan_deadline:
                    break
                scanned += 1
                try:
                    raw = self._backend.read_raw_data(
                        dataset_id=dataset_id,
                        data_id=data_id,
                        timeout=min(30.0, max(1.0, scan_deadline - time.monotonic())),
                    )
                except Exception as exc:
                    logger.debug("raw read of %s failed during forget find: %s", data_id, exc)
                    continue
                lowered = raw.lower()
                matched = [term for term in terms if term in lowered]
                if not matched:
                    continue
                first = lowered.find(matched[0])
                start = max(0, first - self._FORGET_PREVIEW_CHARS // 3)
                preview = raw[start : start + self._FORGET_PREVIEW_CHARS].strip()
                candidates.append(
                    {
                        "data_id": data_id,
                        "name": str(item.get("name") or ""),
                        "matched_terms": matched,
                        "preview": preview,
                    }
                )
            self._record_success()
            candidates.sort(key=lambda c: len(c["matched_terms"]), reverse=True)
            envelope: dict[str, Any] = {
                "action": "find",
                "dataset": dataset,
                "dataset_id": dataset_id,
                "candidates": candidates[: self._FORGET_MAX_CANDIDATES],
                "scanned": scanned,
                "total_items": len(items),
                "note": (
                    "Show these candidates to the user; delete only what they confirm, "
                    "via action='forget' with data_ids and confirm=true. Turns from the "
                    "current conversation become deletable documents only after the "
                    "session is bridged to the graph."
                ),
            }
            if not candidates:
                envelope["result"] = "No stored documents matched those terms."
            return json.dumps(envelope)
        except Exception as exc:
            self._record_failure()
            return json.dumps({"error": f"Cognee forget find failed: {exc}"})

    def _forget_delete(self, args: dict[str, Any]) -> str:
        if not bool(args.get("confirm", False)):
            return json.dumps(
                {"error": "Deletion requires confirm=true, after the user has confirmed."}
            )
        dataset = str(args.get("dataset") or self._dataset)
        if bool(args.get("everything_in_dataset", False)):
            # Whole-dataset deletion stays on the coarse endpoint; the everything
            # (all datasets) wipe is deliberately not expressible from this tool.
            try:
                result = self._backend.forget(
                    dataset=dataset,
                    everything=False,
                    memory_only=False,
                    timeout=self._timeout("write_timeout", 120),
                )
                self._record_success()
                return json.dumps({"result": f"Dataset {dataset!r} deleted.", "details": result})
            except Exception as exc:
                self._record_failure()
                return json.dumps({"error": f"Cognee forget failed: {exc}"})

        data_ids = [str(value) for value in (args.get("data_ids") or []) if str(value).strip()]
        if not data_ids:
            return json.dumps({"error": "action='forget' requires data_ids (from action='find')."})
        try:
            dataset_id = self._resolve_dataset_id(dataset)
            if not dataset_id:
                return json.dumps({"error": f"Dataset {dataset!r} was not found."})
        except Exception as exc:
            self._record_failure()
            return json.dumps({"error": f"Cognee forget failed: {exc}"})

        deleted: list[str] = []
        errors: list[dict[str, str]] = []
        for data_id in data_ids:
            try:
                self._backend.forget_document(
                    dataset_id=dataset_id,
                    data_id=data_id,
                    timeout=self._timeout("write_timeout", 120),
                )
                deleted.append(data_id)
            except Exception as exc:
                if getattr(exc, "status", None) == 404:
                    errors.append({"data_id": data_id, "error": "not found (already deleted?)"})
                else:
                    errors.append({"data_id": data_id, "error": str(exc)[:200]})
        if deleted:
            self._record_success()
        elif errors:
            self._record_failure()
        envelope: dict[str, Any] = {
            "action": "forget",
            "dataset": dataset,
            "deleted": deleted,
            "count": len(deleted),
        }
        if errors:
            envelope["errors"] = errors
        return json.dumps(envelope)

    # -- dataset switching -----------------------------------------------------

    def _apply_dataset_override(self) -> None:
        """Re-apply a persisted per-conversation dataset switch, if any."""
        override = dataset_overrides.load_override(self._session_id)
        if not override:
            return
        try:
            dataset = str(override.get("dataset") or "")
            counter = int(override.get("counter") or 0)
        except (TypeError, ValueError):
            return
        if not dataset or counter <= 0:
            return
        self._dataset = dataset
        self._switch_counter = counter
        self._session_cognee_id = f"{self._session_cognee_id}__{counter}"

    def _handle_switch_dataset(self, args: dict[str, Any]) -> str:
        action = str(args.get("action") or "").strip().lower()
        force = bool(args.get("force", False))
        if action == "current":
            return json.dumps(
                {
                    "dataset": self._dataset,
                    "default": self._default_dataset,
                    "switched": self._dataset != self._default_dataset,
                    "cognee_session_id": self._session_cognee_id,
                }
            )
        if action == "list":
            try:
                rows = self._backend.list_datasets(timeout=30.0)
                self._record_success()
            except Exception as exc:
                self._record_failure()
                return json.dumps({"error": f"Listing datasets failed: {exc}"})
            names = sorted({str(row.get("name") or "") for row in rows} - {""})
            return json.dumps(
                {
                    "current": self._dataset,
                    "default": self._default_dataset,
                    "datasets": [
                        {"name": name, "current": name == self._dataset} for name in names
                    ],
                    "note": (
                        "A name not listed is created on switch. Code-graph datasets "
                        "(codebase-*) belong to indexed repositories — do not move "
                        "conversations into them."
                    ),
                }
            )
        if action == "switch":
            target = str(args.get("dataset") or "").strip()
            if not target:
                return json.dumps({"error": "action='switch' requires a dataset name."})
            return self._switch_dataset(target, force=force)
        if action == "reset":
            return self._switch_dataset(self._default_dataset, force=force)
        return json.dumps({"error": "Specify action: list, current, switch, or reset."})

    def _switch_dataset(self, target: str, *, force: bool) -> str:
        if target == self._dataset:
            return json.dumps({"switched": False, "reason": "already_active", "dataset": target})
        old_dataset, old_session = self._dataset, self._session_cognee_id

        # 1. Flush the turn currently being written, then bridge the session we
        #    are leaving into its dataset. A cognee session never spans two
        #    datasets, so this is the last chance its cached turns have of
        #    reaching the old dataset's graph. run_in_background: the server owns
        #    the promotion; the tool call must not stall on a graph build.
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=10.0)
        bridged = True
        bridge_error = ""
        if self._writes_enabled and self._improve_on_end:
            try:
                self._backend.improve(
                    dataset=old_dataset,
                    session_ids=[old_session],
                    background=True,
                    timeout=self._timeout("improve_timeout", 300),
                )
            except Exception as exc:
                bridged, bridge_error = False, str(exc)[:300]
                if not force:
                    self._record_failure()
                    return json.dumps(
                        {
                            "error": (
                                f"Bridging the current session into {old_dataset!r} failed, "
                                "so nothing was switched. Retry, or pass force=true to "
                                "switch anyway (the session is then re-bridged at session "
                                f"end). Cause: {bridge_error}"
                            )
                        }
                    )
                # Forced past the failure: defer the bridge instead of dropping it.
                self._retired_sessions.append((old_dataset, old_session))

        # 2. Make sure the target exists for this principal (idempotent).
        try:
            self._backend.ensure_dataset(dataset=target, timeout=30.0)
        except Exception as exc:
            self._record_failure()
            return json.dumps(
                {"error": f"Dataset {target!r} is not available to this principal: {exc}"}
            )

        # 3. Re-point capture, recall and the session-end improve under a fresh
        #    cognee session id.
        self._switch_counter += 1
        base = self._build_cognee_session_id(self._session_id)
        self._session_cognee_id = f"{base}__{self._switch_counter}"
        self._dataset = target
        dataset_overrides.save_override(self._session_id, target, self._switch_counter)
        with self._prefetch_lock:
            self._prefetch_result = ""
            self._prefetch_generation += 1
        if self._watcher_state_path is not None and not self._close_handed_off:
            exit_watcher.update(
                self._watcher_state_path,
                session_id=self._session_cognee_id,
                dataset=target,
                improve=bool(self._writes_enabled and self._improve_on_end),
            )
        self._record_success()
        envelope: dict[str, Any] = {
            "switched": True,
            "dataset": target,
            "cognee_session_id": self._session_cognee_id,
            "previous": {"dataset": old_dataset, "session_id": old_session, "bridged": bridged},
        }
        if bridge_error:
            envelope["previous"]["bridge_error"] = bridge_error
        return json.dumps(envelope)

    def _bridge_retired_sessions(self) -> None:
        """Re-submit the bridge for sessions retired by a forced switch."""
        if not self._retired_sessions:
            return
        remaining: list[tuple[str, str]] = []
        for dataset, session_id in self._retired_sessions:
            try:
                self._backend.improve(
                    dataset=dataset,
                    session_ids=[session_id],
                    background=True,
                    timeout=self._timeout("improve_timeout", 300),
                )
            except Exception as exc:
                logger.warning(
                    "re-bridging retired session %s into %s failed: %s", session_id, dataset, exc
                )
                remaining.append((dataset, session_id))
        self._retired_sessions = remaining

    # -- code graph --------------------------------------------------------------

    def _resolve_code_dataset(self, repo: str) -> str:
        """Map a repo path/URL/dataset name — or the cwd — to a code dataset."""
        repo = repo.strip()
        configured = [
            name.strip()
            for name in str(self._config.get("code_datasets") or "").split(",")
            if name.strip()
        ]
        if repo:
            if repo.startswith("codebase-") or repo in configured:
                return repo
            state = code_graph.find_repo_state(repo)
            return str(state.get("dataset") or "")
        state = code_graph.find_indexed_repo(self._cwd)
        if state.get("dataset"):
            return str(state["dataset"])
        return configured[0] if configured else ""

    def _handle_code_search(self, args: dict[str, Any]) -> str:
        operation = str(args.get("operation") or "").strip().lower()
        if operation not in code_graph.CODE_OPERATIONS:
            return json.dumps(
                {
                    "error": f"Unknown operation {operation!r}. One of: "
                    + ", ".join(code_graph.CODE_OPERATIONS)
                }
            )
        name = str(args.get("name") or "").strip()
        target = str(args.get("target") or "").strip()
        limit = min(max(1, int(args.get("limit") or 10)), 50)

        dataset = self._resolve_code_dataset(str(args.get("repo") or ""))
        if not dataset:
            return json.dumps(
                {
                    "error": (
                        "No indexed repository found. Index one first with "
                        "`hermes cognee index-repo <path-or-url>`, or name an indexed "
                        "repo/dataset via the repo parameter."
                    )
                }
            )

        code_query: dict[str, Any] = {"operation": operation}
        if operation == "query_facts":
            if name:
                code_query["name"] = name
            code_query["limit"] = limit
        elif operation == "explore":
            if not name:
                return json.dumps({"error": "explore requires name (the symbol to explore)."})
            code_query["name"] = name
        elif operation == "traverse":
            if not name:
                return json.dumps({"error": "traverse requires name (the seed symbol)."})
            code_query["start"] = name
        elif operation == "find_path":
            if not name or not target:
                return json.dumps({"error": "find_path requires name (source) and target."})
            code_query["source"] = name
            code_query["target"] = target
        elif operation == "impact_analysis":
            if not name:
                return json.dumps({"error": "impact_analysis requires name (the symbol)."})
            code_query["targets"] = [name]

        try:
            results = self._backend.recall(
                query=name or operation,
                session_id=None,
                datasets=[dataset],
                top_k=limit,
                auto_route=True,
                query_type=None,
                scope=["code"],
                code_query=code_query,
                only_context=True,
                timeout=self._timeout("recall_timeout", 120),
            )
            self._record_success()
        except Exception as exc:
            self._record_failure()
            return json.dumps({"error": f"Cognee code search failed: {exc}"})
        items = [_coerce_result_dict(item) for item in results]
        return json.dumps({"dataset": dataset, "results": items, "count": len(items)})

    def _normalize_recall_item(self, item: Any) -> dict[str, Any]:
        data = _coerce_result_dict(item)
        normalized = {
            "text": _result_text(item),
            "source": data.get("source") or data.get("_source") or "cognee",
        }
        for key in ("score", "dataset", "dataset_name", "node_name"):
            if data.get(key) is not None:
                normalized[key] = data[key]
        return normalized

    def _format_recall_lines(self, results: list[Any], *, limit: int) -> list[str]:
        lines = []
        for item in results[:limit]:
            normalized = self._normalize_recall_item(item)
            text = normalized.get("text", "").strip()
            if not text:
                continue
            source = normalized.get("source", "cognee")
            lines.append(f"- [{source}] {text[:500]}")
        return lines

    def _is_breaker_open(self) -> bool:
        with self._breaker_lock:
            if self._consecutive_failures < _BREAKER_THRESHOLD:
                return False
            if time.monotonic() >= self._breaker_open_until:
                self._consecutive_failures = 0
                return False
            return True

    def _record_success(self) -> None:
        with self._breaker_lock:
            self._consecutive_failures = 0

    def _record_failure(self) -> None:
        with self._breaker_lock:
            self._consecutive_failures += 1
            tripped = self._consecutive_failures >= _BREAKER_THRESHOLD
            if tripped:
                self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
                failures = self._consecutive_failures
        if tripped:
            logger.warning(
                "Cognee circuit breaker tripped after %d consecutive failures; pausing for %ds.",
                failures,
                _BREAKER_COOLDOWN_SECS,
            )


def _prompt(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"  {label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        value = ""
    return value or (default or "")


def _prompt_bool(label: str, default: bool) -> bool:
    default_text = "y" if default else "n"
    value = _prompt(f"{label} (y/n)", default=default_text).strip().lower()
    return value in {"y", "yes", "true", "1", "on"}


def _prompt_secret(label: str, *, keep: bool) -> str:
    import getpass
    import sys

    suffix = " (blank to keep current)" if keep else ""
    try:
        if sys.stdin.isatty():
            return getpass.getpass(f"  {label}{suffix}: ").strip()
        return input(f"  {label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""
