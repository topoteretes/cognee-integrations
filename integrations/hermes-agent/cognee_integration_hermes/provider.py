"""Hermes MemoryProvider implementation backed by Cognee."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .config import (
    DEFAULT_DATASET,
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


class _AsyncBridge:
    """Run Cognee async SDK calls from Hermes' sync MemoryProvider interface."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _ensure_loop(self) -> None:
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever,
                daemon=True,
                name="cognee-hermes-event-loop",
            )
            self._thread.start()

    def run(self, coro, timeout: float):
        self._ensure_loop()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def shutdown(self) -> None:
        with self._lock:
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=5.0)
            self._loop = None
            self._thread = None


def _has_cognee() -> bool:
    return importlib.util.find_spec("cognee") is not None


def _safe_session_component(value: str) -> str:
    # Sanitization kept consistent with the other integrations' session-id helpers
    # (claude-code/codex `_sanitize_session_key`, openclaw `sanitizeSessionKey`):
    # keep alphanumerics plus `-` `_` `.`, replace others with `_`, trim `._` ends,
    # cap length at 120.
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)
    return safe.strip("._")[:120] or "session"


def _sanitize_dataset_name(name: str, default: str) -> str:
    """Apply the same character rules as _safe_session_component to a dataset name.

    Keeps ASCII alphanumerics and ``-`` ``_`` ``.'``; replaces everything else
    with ``_``; strips leading/trailing ``'.'`` and ``'_'``; caps at 120 chars.
    Falls back to *default* when the result is empty (e.g. "###" or "___").
    """
    safe = "".join(
        ch if (ch.isascii() and ch.isalnum()) or ch in ("-", "_", ".") else "_"
        for ch in str(name or "")
    )
    return safe.strip("._")[:120] or default


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

    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._bridge = _AsyncBridge()
        self._initialized = False
        self._remote_mode = False
        self._user = None
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
        self._sync_thread: Optional[threading.Thread] = None
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    @property
    def name(self) -> str:
        return "cognee"

    def is_available(self) -> bool:
        if not _has_cognee():
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
        self._dataset = _sanitize_dataset_name(
            str(self._config.get("dataset") or ""), DEFAULT_DATASET
        )
        self._top_k = int(self._config.get("top_k") or 5)
        self._auto_route = str_to_bool(self._config.get("auto_route"), True)
        self._improve_on_end = str_to_bool(self._config.get("improve_on_end"), True)
        self._writes_enabled = kwargs.get("agent_context", "primary") in {"", "primary", None}
        self._session_cognee_id = self._build_cognee_session_id(session_id, **kwargs)

        self._configure_cognee_models()

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
                self._bridge.run(self._do_serve(service_url, api_key), timeout=30)
                self._remote_mode = True
            except Exception as exc:
                raise RuntimeError(
                    f"COGNEE_BASE_URL is set to {service_url!r} but the connection failed. "
                    "Fix the URL/network/credentials, or unset it to use local mode."
                ) from exc
        elif embedded:
            self._configure_cognee_local_roots()
            self._remote_mode = False
        else:
            try:
                local_url = ensure_local_server(
                    int(self._config.get("local_port") or 8000),
                    data_root=str(self._config.get("data_root") or ""),
                    system_root=str(self._config.get("system_root") or ""),
                    boot_timeout=float(self._config.get("server_boot_timeout", 30)),
                )
                self._bridge.run(self._do_serve(local_url, ""), timeout=30)
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
        if self._remote_mode:
            self._user = None
        else:
            try:
                self._user = self._bridge.run(self._ensure_identity(), timeout=30)
            except Exception as exc:
                self._user = None
                logger.warning(
                    "Cognee identity initialization failed; using SDK default user: %s", exc
                )

        self._initialized = True

    def system_prompt_block(self) -> str:
        mode = "remote" if self._remote_mode else "local"
        return (
            "# Cognee Memory\n"
            f"Active ({mode}). Dataset: {self._dataset}.\n"
            "Use cognee_recall for prior context, cognee_remember for durable facts, "
            "and cognee_forget when the user asks to remove Cognee memory."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        if not result:
            return ""
        return f"## Cognee Memory\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not query or self._is_breaker_open():
            return

        cognee_session_id = self._session_cognee_id_for(session_id)

        def _run() -> None:
            try:
                results = self._bridge.run(
                    self._do_recall(query, None, min(self._top_k, 5), "auto", cognee_session_id),
                    timeout=float(self._config.get("recall_timeout", 60)),
                )
                lines = self._format_recall_lines(results, limit=5)
                if lines:
                    with self._prefetch_lock:
                        self._prefetch_result = "\n".join(lines)
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
        if not self._writes_enabled or self._is_breaker_open():
            return

        cognee_session_id = self._session_cognee_id_for(session_id)
        content = _format_turn(user_content, assistant_content)

        def _sync() -> None:
            try:
                self._bridge.run(
                    self._do_remember_session(content, cognee_session_id),
                    timeout=float(self._config.get("write_timeout", 120)),
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
        if not self._writes_enabled or not self._improve_on_end or self._is_breaker_open():
            return
        # When to background the graph-build: only when a server will outlive this
        # process and finish the job. In embedded mode the work runs in-process, so
        # it must complete synchronously before shutdown or it is lost. Override via
        # COGNEE_IMPROVE_BACKGROUND.
        raw_bg = str(self._config.get("improve_background") or "").strip()
        background = str_to_bool(raw_bg, self._remote_mode) if raw_bg else self._remote_mode
        try:
            self._bridge.run(
                self._do_improve(run_in_background=background),
                timeout=float(self._config.get("improve_timeout", 300)),
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

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if action not in {"add", "replace"} or not content or self._is_breaker_open():
            return
        metadata = dict(metadata or {})
        source = metadata.get("write_origin") or "hermes_memory_tool"
        payload = f"Hermes {target} memory ({action}, {source}): {content}"

        def _sync() -> None:
            try:
                self._bridge.run(
                    self._do_remember_permanent(payload, self._dataset),
                    timeout=float(self._config.get("write_timeout", 120)),
                )
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
        if self._remote_mode:
            try:
                self._bridge.run(self._do_disconnect(), timeout=5)
            except Exception:
                pass
        self._bridge.shutdown()

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

    def _configure_cognee_local_roots(self) -> None:
        if not self._hermes_home:
            return
        try:
            import cognee

            data_root = self._config.get("data_root") or str(
                Path(self._hermes_home) / "cognee" / "data"
            )
            system_root = self._config.get("system_root") or str(
                Path(self._hermes_home) / "cognee" / "system"
            )
            cognee.config.data_root_directory(str(data_root))
            cognee.config.system_root_directory(str(system_root))
        except Exception as exc:
            logger.debug("Cognee root configuration failed: %s", exc)

    def _configure_cognee_models(self) -> None:
        try:
            import cognee

            if self._config.get("llm_api_key"):
                cognee.config.set_llm_api_key(str(self._config["llm_api_key"]))
            if self._config.get("llm_model"):
                cognee.config.set_llm_model(str(self._config["llm_model"]))
        except Exception as exc:
            logger.debug("Cognee model configuration failed: %s", exc)

    async def _ensure_identity(self):
        email = str(self._config.get("identity_email") or "hermes-agent@cognee.local")
        password = str(self._config.get("identity_password") or "hermes-agent-plugin")
        try:
            from cognee.modules.users.methods import (
                create_user,
                get_default_user,
                get_user_by_email,
            )
        except Exception:
            return None

        user = await get_user_by_email(email)
        if user:
            return user
        try:
            return await create_user(
                email=email,
                password=password,
                is_verified=True,
                is_active=True,
            )
        except Exception:
            user = await get_user_by_email(email)
            if user:
                return user
            return await get_default_user()

    async def _do_serve(self, url: str, api_key: str):
        import cognee

        kwargs = {"url": url}
        if api_key:
            kwargs["api_key"] = api_key
        return await cognee.serve(**kwargs)

    async def _do_disconnect(self):
        import cognee

        return await cognee.disconnect()

    def _add_user_kwarg(self, kwargs: dict[str, Any]) -> None:
        """Inject the local user only in embedded mode.

        In server/remote mode the server owns identity (api-key principal) and
        ``self._user`` is None. Passing ``user=None`` is not the same as omitting
        the key — the SDK may treat an explicit None differently (overriding a
        default, affecting tenant scoping) — so we omit it entirely instead.
        """
        if not self._remote_mode and self._user is not None:
            kwargs["user"] = self._user

    async def _do_recall(
        self,
        query: str,
        search_type: Optional[str],
        top_k: int,
        scope: str,
        session_id: str,
    ) -> list[Any]:
        import cognee

        kwargs: dict[str, Any] = {
            "top_k": top_k,
            "auto_route": self._auto_route,
        }
        self._add_user_kwarg(kwargs)

        normalized_scope = (scope or "auto").lower()
        if normalized_scope == "session":
            kwargs["session_id"] = session_id
        elif normalized_scope == "graph":
            kwargs["datasets"] = [self._dataset]
        else:
            kwargs["session_id"] = session_id
            kwargs["datasets"] = [self._dataset]

        if search_type and normalized_scope != "session":
            kwargs["query_type"] = _resolve_search_type(search_type)

        return await cognee.recall(query_text=query, **kwargs)

    async def _do_remember_session(self, content: str, session_id: str):
        import cognee

        kwargs: dict[str, Any] = {
            "data": content,
            "dataset_name": self._dataset,
            "session_id": session_id,
            "self_improvement": False,
        }
        self._add_user_kwarg(kwargs)
        return await cognee.remember(**kwargs)

    async def _do_remember_permanent(self, content: str, dataset: str):
        import cognee

        kwargs: dict[str, Any] = {
            "data": content,
            "dataset_name": dataset,
            "self_improvement": True,
            "session_ids": [self._session_cognee_id],
        }
        self._add_user_kwarg(kwargs)
        return await cognee.remember(**kwargs)

    async def _do_forget(
        self,
        dataset: Optional[str],
        *,
        everything: bool = False,
        memory_only: bool = False,
    ) -> dict[str, Any]:
        import cognee

        kwargs: dict[str, Any] = {
            "everything": everything,
            "memory_only": memory_only,
        }
        if dataset and not everything:
            kwargs["dataset"] = dataset
        self._add_user_kwarg(kwargs)
        return await cognee.forget(**kwargs)

    async def _do_improve(self, run_in_background: bool = False):
        # Default stays False (synchronous) so the method contract is unchanged for
        # any caller that relies on completion. on_session_end() chooses the flag.
        import cognee

        kwargs: dict[str, Any] = {
            "dataset": self._dataset,
            "session_ids": [self._session_cognee_id],
            "run_in_background": run_in_background,
        }
        self._add_user_kwarg(kwargs)
        return await cognee.improve(**kwargs)

    def _handle_recall(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return json.dumps({"error": "Missing required parameter: query"})
        top_k = min(max(1, int(args.get("top_k") or self._top_k)), 20)
        scope = str(args.get("scope") or "auto")
        search_type = args.get("search_type")

        try:
            results = self._bridge.run(
                self._do_recall(query, search_type, top_k, scope, self._session_cognee_id),
                timeout=float(self._config.get("recall_timeout", 60)),
            )
            self._record_success()
            items = [self._normalize_recall_item(item) for item in results]
            if not items:
                return json.dumps({"result": "No relevant Cognee memory found.", "count": 0})
            return json.dumps({"results": items, "count": len(items)})
        except Exception as exc:
            self._record_failure()
            return json.dumps({"error": f"Cognee recall failed: {exc}"})

    def _handle_remember(self, args: dict[str, Any]) -> str:
        content = str(args.get("content") or "").strip()
        if not content:
            return json.dumps({"error": "Missing required parameter: content"})
        dataset = _sanitize_dataset_name(str(args.get("dataset") or ""), self._dataset)

        try:
            result = self._bridge.run(
                self._do_remember_permanent(content, dataset),
                timeout=float(self._config.get("write_timeout", 120)),
            )
            self._record_success()
            status = getattr(result, "status", "completed")
            return json.dumps({"result": "Content stored in Cognee.", "status": str(status)})
        except Exception as exc:
            self._record_failure()
            return json.dumps({"error": f"Cognee remember failed: {exc}"})

    def _handle_forget(self, args: dict[str, Any]) -> str:
        _raw_dataset = args.get("dataset")
        dataset = _sanitize_dataset_name(str(_raw_dataset), self._dataset) if _raw_dataset else None
        everything = bool(args.get("everything", False))
        memory_only = bool(args.get("memory_only", False))
        if not dataset and not everything:
            return json.dumps({"error": "Specify dataset or set everything=true."})

        try:
            result = self._bridge.run(
                self._do_forget(dataset, everything=everything, memory_only=memory_only),
                timeout=float(self._config.get("write_timeout", 120)),
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


def _resolve_search_type(search_type: str):
    try:
        from cognee.modules.search.types import SearchType

        key = str(search_type).upper().strip()
        return getattr(SearchType, key)
    except Exception:
        from cognee.modules.search.types import SearchType

        return SearchType.GRAPH_COMPLETION


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
