"""Hermes MemoryProvider implementation backed by Cognee."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .backend import MemoryBackend, build_backend, default_backend, has_cognee
from .config import (
    DEFAULT_DATASET,
    DEFAULT_IDENTITY_EMAIL,
    DEFAULT_IDENTITY_PASSWORD,
    load_config,
    str_to_bool,
    write_env_vars,
)
from .config import (
    save_config as save_plugin_config,
)
from .schemas import FORGET_SCHEMA, RECALL_SCHEMA, REMEMBER_SCHEMA
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
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

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
                "description": "Default Cognee dataset",
                "default": DEFAULT_DATASET,
                "env_var": "COGNEE_DATASET",
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
        self._dataset = str(self._config.get("dataset") or DEFAULT_DATASET)
        self._top_k = int(self._config.get("top_k") or 5)
        self._auto_route = str_to_bool(self._config.get("auto_route"), True)
        self._improve_on_end = str_to_bool(self._config.get("improve_on_end"), True)
        self._writes_enabled = kwargs.get("agent_context", "primary") in {"", "primary", None}
        self._session_cognee_id = self._build_cognee_session_id(session_id, **kwargs)

        # Now that config is loaded, choose the transport (unless one was injected).
        if self._injected_backend is None:
            self._backend = build_backend(self._config, hermes_home=self._hermes_home or "")

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
                local_url = ensure_local_server(
                    int(self._config.get("local_port") or 8000),
                    data_root=str(self._config.get("data_root") or ""),
                    system_root=str(self._config.get("system_root") or ""),
                    boot_timeout=float(self._config.get("server_boot_timeout", 30)),
                )
                self._backend.connect(url=local_url, api_key="", timeout=30)
                self._remote_mode = True
            except Exception as exc:
                raise RuntimeError(
                    "cognee local server failed to start, which is required for safe "
                    "concurrent DB access. Check for a port conflict on "
                    f"{self._config.get('local_port') or 8000}, missing dependencies "
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

        self._initialized = True

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
        return (
            "# Cognee Memory\n"
            f"Active ({mode}). Dataset: {self._dataset}.\n"
            "Use cognee_recall for prior context, cognee_remember for durable facts, "
            "and cognee_forget when the user asks to remove Cognee memory."
        )

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
            try:
                results = self._recall(
                    query,
                    scope="auto",
                    search_type=None,
                    top_k=min(self._top_k, 5),
                    session_id=cognee_session_id,
                )
                lines = self._format_recall_lines(results, limit=5)
                if lines:
                    with self._prefetch_lock:
                        # Drop the result if a reset invalidated it mid-recall.
                        if generation == self._prefetch_generation:
                            self._prefetch_result = "\n".join(lines)
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
        return [RECALL_SCHEMA, REMEMBER_SCHEMA, FORGET_SCHEMA]

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
        # When to background the graph-build: only when a server will outlive this
        # process and finish the job. In embedded mode the work runs in-process, so
        # it must complete synchronously before shutdown or it is lost. Override via
        # COGNEE_IMPROVE_BACKGROUND.
        raw_bg = str(self._config.get("improve_background") or "").strip()
        background = str_to_bool(raw_bg, self._remote_mode) if raw_bg else self._remote_mode
        try:
            self._backend.improve(
                dataset=self._dataset,
                session_ids=[self._session_cognee_id],
                background=background,
                timeout=self._timeout("improve_timeout", 300),
            )
            self._record_success()
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
        self._session_cognee_id = self._build_cognee_session_id(new_session_id, **kwargs)
        if reset:
            with self._prefetch_lock:
                self._prefetch_result = ""
                # Invalidate any recall still in flight for the old conversation.
                # Only on reset: /resume, /branch and compression continue the same
                # logical conversation, so a prefetch issued for it stays valid.
                self._prefetch_generation += 1

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

    def _configure_local_roots(self) -> None:
        """Point the backend at profile-scoped storage (embedded mode only)."""
        if not self._hermes_home:
            return
        data_root = self._config.get("data_root") or str(
            Path(self._hermes_home) / "cognee" / "data"
        )
        system_root = self._config.get("system_root") or str(
            Path(self._hermes_home) / "cognee" / "system"
        )
        self._backend.configure_local_roots(data_root=str(data_root), system_root=str(system_root))

    def _timeout(self, key: str, default: float) -> float:
        """A named, bounded timeout read from config at call time."""
        return float(self._config.get(key, default))

    def _recall_scope_params(
        self, scope: str, search_type: Any, session_id: str
    ) -> tuple[Optional[str], Optional[list[str]], Optional[str]]:
        """Map the tool's ``scope`` onto the backend's explicit targets.

        ``session`` searches only this conversation's cache, ``graph`` only the
        permanent dataset, ``auto`` both. A ``search_type`` override is
        meaningless for a pure session lookup, so it is dropped there.
        """
        normalized = (scope or "auto").lower()
        if normalized == "session":
            return session_id, None, None
        query_type = search_type or None
        if normalized == "graph":
            return None, [self._dataset], query_type
        return session_id, [self._dataset], query_type

    def _recall(
        self,
        query: str,
        *,
        scope: str,
        search_type: Any,
        top_k: int,
        session_id: str,
    ) -> list[Any]:
        target_session, datasets, query_type = self._recall_scope_params(
            scope, search_type, session_id
        )
        return self._backend.recall(
            query=query,
            session_id=target_session,
            datasets=datasets,
            top_k=top_k,
            auto_route=self._auto_route,
            query_type=query_type,
            timeout=self._timeout("recall_timeout", 120),
        )

    def _remember_permanent(self, content: str, dataset: str) -> Any:
        return self._backend.remember_permanent(
            text=content,
            dataset=dataset,
            session_ids=[self._session_cognee_id],
            timeout=self._timeout("write_timeout", 120),
        )

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
            if not items:
                # Distinguish a genuine miss from a backend condition that makes
                # recall structurally unable to match (e.g. an embedder change
                # leaving stored and query vectors different sizes). A confirmed
                # cause is a hard error rather than a silent empty result.
                dim_message = self._backend.empty_recall_hint()
                if dim_message:
                    return json.dumps({"error": dim_message, "count": 0})
                return json.dumps({"result": "No relevant Cognee memory found.", "count": 0})
            return json.dumps({"results": items, "count": len(items)})
        except Exception as exc:
            self._record_failure()
            return json.dumps({"error": f"Cognee recall failed: {exc}"})

    def _handle_remember(self, args: dict[str, Any]) -> str:
        content = str(args.get("content") or "").strip()
        if not content:
            return json.dumps({"error": "Missing required parameter: content"})
        dataset = str(args.get("dataset") or self._dataset)

        try:
            result = self._remember_permanent(content, dataset)
            self._record_success()
            status = getattr(result, "status", "completed")
            return json.dumps({"result": "Content stored in Cognee.", "status": str(status)})
        except Exception as exc:
            self._record_failure()
            return json.dumps({"error": f"Cognee remember failed: {exc}"})

    def _handle_forget(self, args: dict[str, Any]) -> str:
        dataset = args.get("dataset")
        everything = bool(args.get("everything", False))
        memory_only = bool(args.get("memory_only", False))
        if not dataset and not everything:
            return json.dumps({"error": "Specify dataset or set everything=true."})

        try:
            result = self._backend.forget(
                dataset=dataset,
                everything=everything,
                memory_only=memory_only,
                timeout=self._timeout("write_timeout", 120),
            )
            self._record_success()
            return json.dumps({"result": "Cognee memory deleted.", "details": result})
        except Exception as exc:
            self._record_failure()
            return json.dumps({"error": f"Cognee forget failed: {exc}"})

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
        if self._consecutive_failures < _BREAKER_THRESHOLD:
            return False
        if time.monotonic() >= self._breaker_open_until:
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self) -> None:
        self._consecutive_failures = 0

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            logger.warning(
                "Cognee circuit breaker tripped after %d consecutive failures; pausing for %ds.",
                self._consecutive_failures,
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
