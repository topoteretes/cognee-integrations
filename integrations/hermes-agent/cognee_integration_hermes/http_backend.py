"""Direct-HTTP transport to a cognee server — stdlib only.

This is the transport the other cognee plugins use (claude-code's
``scripts/_cognee_client.py``, openclaw's ``src/client.ts``): talk to the server's
own REST API and own the request bodies. The alternative, routing through
``cognee.serve()``'s ``CloudClient``, silently drops fields the server accepts —
most importantly ``session_ids`` on ``improve()``, which is what bridges session
memory into the permanent graph.

**Wire contract** (verified against cognee 1.2.1's routers, live-checked on the
pinned 1.4.0):

===================  =========================================================
``recall``           ``POST /api/v1/recall``   JSON: ``query``, ``search_type``,
                     ``datasets``, ``top_k``, ``session_id``
``remember_session`` ``POST /api/v1/remember`` multipart: ``data``,
                     ``datasetName``, ``session_id``
``remember_permanent`` ``POST /api/v1/remember`` multipart: ``data``,
                     ``datasetName`` (no session -> direct add+cognify)
``improve``          ``POST /api/v1/improve``  JSON: ``dataset_name``,
                     ``session_ids``, ``run_in_background``
``forget``           ``POST /api/v1/forget``   JSON: ``dataset``, ``everything``,
                     ``memory_only``
===================  =========================================================

Note the field-name differences from the SDK: the endpoint calls them ``query``
and ``search_type`` where the SDK says ``query_text`` and ``query_type``.

**Fields the HTTP API does not have:**

* ``auto_route`` — no such field on ``/api/v1/recall``, but the setting is still
  honoured: ``auto_route=False`` is translated into an explicit
  ``search_type=GRAPH_COMPLETION``, which is what it means server-side. See
  :meth:`recall`.
* ``session_ids`` on a *permanent* write — no such field on ``/api/v1/remember``
  and no equivalent, so a graph write cannot be linked to its session. Logged
  once rather than dropped silently. ``improve`` does accept ``session_ids``, so
  the session-to-graph bridge itself is unaffected.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .backend import MemoryBackend
from .config import SHARED_PLUGIN_STATE_DIR

logger = logging.getLogger(__name__)

DEFAULT_USER_EMAIL = "default_user@example.com"
DEFAULT_USER_PASSWORD = "default_password"
_API_KEY_NAME = "hermes-owner-bootstrap"


class CogneeHttpError(RuntimeError):
    """The server was reached and rejected the request.

    ``status`` is carried so a caller can eventually distinguish a
    misconfiguration (4xx — waiting will not help) from server trouble (5xx).
    """

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class CogneeUnreachable(RuntimeError):
    """The server could not be reached at all (DNS, connection, timeout)."""


class RememberResponse:
    """A remember result that exposes ``.status`` like the SDK's ``RememberResult``.

    The provider reads ``getattr(result, "status", "completed")``, so returning the
    raw response dict here would silently report "completed" for every write.
    """

    def __init__(self, payload: dict[str, Any]):
        self.raw = payload
        self.status = str(payload.get("status") or "completed")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RememberResponse(status={self.status!r})"


def _multipart_body(fields: dict[str, str], files: dict[str, tuple[str, bytes]]):
    """Encode a multipart/form-data body. Returns ``(content_type, body_bytes)``."""
    boundary = f"----cognee-hermes-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            ).encode("utf-8")
        )
    for name, (filename, content) in files.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                "Content-Type: text/plain\r\n\r\n"
            ).encode("utf-8")
        )
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


class HttpBackend(MemoryBackend):
    """Talks to a cognee server over its REST API.

    ``cache_dir`` is where a minted API key is remembered. It defaults to
    ``~/.cognee-plugin``, the state dir the claude-code/codex/openclaw plugins
    share — one principal key (``api_key.json``) serves every cognee plugin on
    the machine, in their exact file format. Override it only in tests.
    """

    def __init__(
        self,
        *,
        cache_dir: Optional[str] = None,
        opener=None,
        agent_session_name: str = "hermes",
    ):
        self.url = ""
        self.api_key = ""
        self.registered = False
        self._cache_dir = Path(cache_dir).expanduser() if cache_dir else SHARED_PLUGIN_STATE_DIR
        self._opener = opener
        self._agent_session_name = agent_session_name
        self._ssl_context: Optional[ssl.SSLContext] = None
        self._warned: set[str] = set()

    # -- transport ---------------------------------------------------------

    def _context(self) -> ssl.SSLContext:
        if self._ssl_context is None:
            self._ssl_context = ssl.create_default_context()
        return self._ssl_context

    def _open(self, request: urllib.request.Request, timeout: float):
        if self._opener is not None:
            return self._opener(request, timeout=timeout)
        return urllib.request.urlopen(request, timeout=timeout, context=self._context())

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        json_body: Optional[dict[str, Any]] = None,
        form_body: Optional[dict[str, str]] = None,
        multipart: Optional[tuple[str, bytes]] = None,
        cookies: Optional[dict[str, str]] = None,
        base_url: str = "",
    ) -> Any:
        """Make one request and return its parsed JSON body (``None`` if empty)."""
        url = (base_url or self.url).rstrip("/") + path
        headers: dict[str, str] = {}
        data: Optional[bytes] = None

        if json_body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(json_body).encode("utf-8")
        elif form_body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = urllib.parse.urlencode(form_body).encode("utf-8")
        elif multipart is not None:
            headers["Content-Type"], data = multipart

        # Always send the key when we have one: cognee enforces auth on its API
        # routes even on localhost, and a server with auth disabled ignores it.
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._open(request, timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise CogneeHttpError(
                exc.code, f"{method} {path} failed (HTTP {exc.code}): {detail or exc.reason}"
            ) from exc
        except Exception as exc:  # URLError / timeout / OSError
            raise CogneeUnreachable(f"cognee unreachable at {url}: {str(exc)[:200]}") from exc

        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            # A body we cannot parse is a server-side problem, not unreachability.
            raise CogneeHttpError(200, f"malformed JSON from {path}: {str(exc)[:160]}") from exc

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(message)

    # -- connection / lifecycle -------------------------------------------

    def connect(self, *, url: str, api_key: str, timeout: float) -> None:
        """Health-check *url*, resolve an API key, and register this connection.

        The health check is what makes an unreachable target a hard failure.
        ``cognee.serve()`` only logged a warning and handed back a client, so a bad
        ``COGNEE_BASE_URL`` looked like success until the first real call.
        """
        self.url = url.rstrip("/")
        self.api_key = ""

        health = self._request("GET", "/health", timeout=min(timeout, 10.0))
        del health  # any 2xx is healthy; body shape is not part of the contract

        self.api_key = self._resolve_api_key(api_key, timeout=timeout)

        # Registration drives the server's idle-shutdown watchdog. Useful, but not
        # required for correctness — never fail a session over it.
        try:
            self._request(
                "POST",
                "/api/v1/agents/register",
                timeout=timeout,
                json_body={
                    "agent_session_name": self._agent_session_name,
                    "type": "api",
                    "source": "api",
                },
            )
            self.registered = True
        except Exception as exc:
            logger.debug("cognee agent registration failed (continuing): %s", exc)

    def close(self, *, timeout: float = 5.0) -> None:
        if not self.registered:
            return
        try:
            self._request(
                "POST",
                "/api/v1/agents/unregister",
                timeout=timeout,
                json_body={"agent_session_name": self._agent_session_name},
            )
        except Exception as exc:
            logger.debug("cognee agent unregistration failed: %s", exc)
        finally:
            self.registered = False

    # -- auth --------------------------------------------------------------

    def _key_cache_path(self) -> Path:
        # Name and shape are shared with the other cognee plugins (claude-code's
        # ``_API_KEY_CACHE``): whichever plugin mints first, the rest reuse.
        return self._cache_dir / "api_key.json"

    @staticmethod
    def _normalize_url(value: str) -> str:
        return str(value or "").strip().rstrip("/")

    def _cached_key(self) -> str:
        path = self._key_cache_path()
        if not path.exists():
            return ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        key = str(data.get("api_key") or "").strip()
        if not key:
            return ""
        # Same match rule as the other plugins: a recorded base_url only
        # invalidates the key when both sides are non-empty and differ.
        cached_url = self._normalize_url(str(data.get("base_url") or ""))
        wanted = self._normalize_url(self.url)
        if wanted and cached_url and cached_url != wanted:
            return ""
        return key

    def _cache_key(self, api_key: str) -> None:
        path = self._key_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "base_url": self._normalize_url(self.url),
                        "api_key": api_key.strip(),
                        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                ),
                encoding="utf-8",
            )
            path.chmod(0o600)
        except OSError as exc:
            logger.debug("could not cache the cognee API key: %s", exc)

    def _resolve_api_key(self, provided: str, *, timeout: float) -> str:
        """Configured key, else a cached one, else mint one from the default user."""
        key = (provided or os.environ.get("COGNEE_API_KEY", "")).strip()
        if key:
            return key
        key = self._cached_key()
        if key:
            return key
        try:
            key = self._mint_api_key(timeout=timeout)
        except Exception as exc:
            # A server with authentication disabled needs no key at all, so this
            # is not fatal — proceed unauthenticated and let the first real call
            # report a 401 if the server does want one.
            logger.debug("could not mint a cognee API key (continuing without): %s", exc)
            return ""
        if key:
            self._cache_key(key)
        return key

    def _mint_api_key(self, *, timeout: float) -> str:
        """Log in as the default user and reuse or create an owner API key."""
        email = os.environ.get("COGNEE_USER_EMAIL", DEFAULT_USER_EMAIL)
        password = os.environ.get("COGNEE_USER_PASSWORD", DEFAULT_USER_PASSWORD)

        login = self._request(
            "POST",
            "/api/v1/auth/login",
            timeout=timeout,
            form_body={"username": email, "password": password},
        )
        token = str((login or {}).get("access_token") or "")
        if not token:
            raise CogneeHttpError(200, "login returned no access token")
        cookies = {"auth_token": token}

        existing = self._request("GET", "/api/v1/auth/api-keys", timeout=timeout, cookies=cookies)
        if isinstance(existing, list) and existing:
            key = str(existing[0].get("key") or "")
            if key:
                return key

        created = self._request(
            "POST",
            "/api/v1/auth/api-keys",
            timeout=timeout,
            json_body={"name": _API_KEY_NAME},
            cookies=cookies,
        )
        return str((created or {}).get("key") or "")

    # -- operations --------------------------------------------------------

    def recall(
        self,
        *,
        query,
        session_id,
        datasets,
        top_k,
        auto_route,
        query_type,
        timeout,
    ) -> list[Any]:
        if not auto_route and not query_type:
            # /api/v1/recall has no auto_route field, but the setting is still
            # expressible: server-side, ``auto_route=False`` with no explicit type
            # means "skip the query classifier and use GRAPH_COMPLETION"
            # (recall.py:519). Naming that type directly bypasses the classifier
            # too, so this is the same retrieval path — only cognee's
            # router-override counter differs, which is pure telemetry.
            query_type = "GRAPH_COMPLETION"

        body: dict[str, Any] = {"query": query, "top_k": top_k}
        if session_id:
            body["session_id"] = session_id
        if datasets:
            body["datasets"] = datasets
        if query_type:
            # The endpoint calls this ``search_type``; an unknown name is rejected
            # by the server's enum, so fall back the way the SDK transport does.
            body["search_type"] = str(query_type).upper().strip()

        result = self._request("POST", "/api/v1/recall", timeout=timeout, json_body=body)
        if isinstance(result, dict):
            if result.get("error"):
                raise CogneeHttpError(200, str(result["error"])[:200])
            return [result]
        return result or []

    def _remember(
        self, *, text: str, dataset: str, session_id: str, timeout: float
    ) -> RememberResponse:
        fields = {"datasetName": dataset}
        if session_id:
            fields["session_id"] = session_id
        multipart = _multipart_body(fields, {"data": ("memory.txt", text.encode("utf-8"))})
        payload = self._request("POST", "/api/v1/remember", timeout=timeout, multipart=multipart)
        return RememberResponse(payload if isinstance(payload, dict) else {})

    def remember_session(self, *, text, session_id, dataset, timeout) -> RememberResponse:
        """Store a turn in the session cache as a typed QA entry.

        This has to go through ``/api/v1/remember/entry``, not ``/api/v1/remember``
        with a ``session_id``. Live-diagnosed: the document endpoint takes its
        payload as a multipart file, which the server coerces to a placeholder
        (``[UploadFile]``), and ``_add_to_session`` deliberately skips those
        because they are useless in a session cache. The write reported
        ``status: "session_stored"`` and stored nothing, so turns silently vanished
        and ``improve()`` had nothing to promote. The typed-entry endpoint is what
        the Claude Code plugin uses for the same purpose.

        The turn arrives pre-framed as ``User: …\\nAssistant: …``, which the QA
        shape cannot split back apart, so it goes in the ``answer`` field — exactly
        what cognee's own SDK does for a session write (``add_qa`` with an empty
        question).
        """
        payload = self._request(
            "POST",
            "/api/v1/remember/entry",
            timeout=timeout,
            json_body={
                "entry": {"type": "qa", "question": "", "answer": text, "context": ""},
                "dataset_name": dataset,
                "session_id": session_id,
            },
        )
        return RememberResponse(payload if isinstance(payload, dict) else {})

    def remember_permanent(self, *, text, dataset, session_ids, timeout) -> RememberResponse:
        if session_ids:
            self._warn_once(
                "remember_session_ids",
                "session_ids on a permanent write is not supported over HTTP: "
                "/api/v1/remember has no such field, so the graph write is not linked "
                "to the session. The session-to-graph bridge (improve) is unaffected.",
            )
        # No session_id -> the server does a direct add + cognify.
        return self._remember(text=text, dataset=dataset, session_id="", timeout=timeout)

    def forget(self, *, dataset, everything, memory_only, timeout) -> dict[str, Any]:
        body: dict[str, Any] = {"everything": everything, "memory_only": memory_only}
        if dataset and not everything:
            body["dataset"] = dataset
        result = self._request("POST", "/api/v1/forget", timeout=timeout, json_body=body)
        return result if isinstance(result, dict) else {}

    def improve(self, *, dataset, session_ids, background, timeout) -> Any:
        # ``session_ids`` is the whole point of this transport: it is what bridges
        # session-cache content into the permanent graph, and the SDK's CloudClient
        # never sends it.
        body: dict[str, Any] = {
            "dataset_name": dataset,
            "run_in_background": background,
        }
        if session_ids:
            body["session_ids"] = session_ids
        return self._request("POST", "/api/v1/improve", timeout=timeout, json_body=body)
