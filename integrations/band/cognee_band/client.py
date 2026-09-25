"""Thin, stdlib-only HTTP client for the Cognee server API.

Ports the request contracts already used by the Claude Code / Codex plugins:

  * ``POST /api/v1/recall``          — context recall (dataset + session scoped)
  * ``POST /api/v1/remember/entry``  — typed QA/trace entry into the session cache
  * ``POST /api/v1/remember``        — durable document write (explicit remember)
  * ``POST /api/v1/improve``         — bridge a session cache into the graph

Error contract (same as the existing plugins): an HTTP error from a reachable
server is reported as an error envelope and is NOT treated as "no results";
only a transport failure counts as unreachable. All methods are synchronous —
the adapter wraps them in ``asyncio.to_thread`` so Band's event loop never
blocks on memory I/O.
"""

import json
import ssl
import sys
import urllib.error
import urllib.request
import uuid

from .config import CogneeSettings

UNREACHABLE = "UNREACHABLE"


def _build_https_opener():
    # macOS Python installs often lack root CAs in the default bundle; prefer
    # certifi when importable, else fall back to the default context.
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


_HTTPS_OPENER = _build_https_opener()


def _error(status, message):
    return {"error": message, "status": status, "authoritative": False}


class CogneeClient:
    """Synchronous client bound to one server + dataset."""

    def __init__(self, settings: CogneeSettings, *, opener=None):
        self.settings = settings
        self._open = opener if opener is not None else _HTTPS_OPENER.open

    # -- plumbing ------------------------------------------------------------

    def _headers(self, content_type="application/json"):
        headers = {"Content-Type": content_type}
        # A server with auth disabled ignores the header; cognee >=1.2.2
        # enforces auth even on localhost.
        if self.settings.api_key:
            headers["X-Api-Key"] = self.settings.api_key
        return headers

    def _post_json(self, path, payload, timeout):
        url = self.settings.base_url.rstrip("/") + path
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with self._open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                msg = f"unauthorized (HTTP {e.code}) — check COGNEE_API_KEY"
            else:
                msg = f"server returned HTTP {e.code} for {path}"
            sys.stderr.write(f"[cognee-band] {msg}\n")
            return _error(e.code, msg)
        except Exception as e:
            sys.stderr.write(
                f"[cognee-band] server unreachable at {self.settings.base_url}: {str(e)[:160]}\n"
            )
            return UNREACHABLE
        try:
            return json.loads(raw or "{}")
        except (json.JSONDecodeError, ValueError):
            return _error(200, f"malformed JSON response from {path}")

    # -- recall ----------------------------------------------------------------

    def recall(self, query: str, *, session_id: str = "", top_k: int = 0):
        """Search memory. Returns a list of results (empty list is authoritative:
        the server searched and found nothing), an error envelope, or UNREACHABLE.
        """
        body = {
            "query": query,
            "top_k": top_k or self.settings.top_k,
            "only_context": True,
            "scope": "auto",
            # All plugin writes target one dataset; searching elsewhere only
            # adds noise from unrelated sessions or SDK calls.
            "datasets": [self.settings.dataset],
        }
        if session_id:
            body["session_id"] = session_id
        result = self._post_json("/api/v1/recall", body, self.settings.recall_timeout)
        if result == UNREACHABLE or (isinstance(result, dict) and result.get("error")):
            return result
        return result if isinstance(result, list) else [result]

    # -- session-cache writes ----------------------------------------------------

    def store_qa(self, question: str, answer: str, *, session_id: str, context: str = ""):
        """Store one paired prompt/answer row in the server session cache."""
        entry = {"type": "qa", "question": question, "answer": answer, "context": context}
        return self._store_entry(entry, session_id)

    def store_trace(
        self,
        origin_function: str,
        params,
        return_value,
        *,
        session_id: str,
        status: str = "success",
        error_message: str = "",
    ):
        """Store one tool-call trace entry in the server session cache."""
        entry = {
            "type": "trace",
            "origin_function": origin_function,
            "status": status,
            "method_params": params,
            "method_return_value": return_value,
            "error_message": error_message,
            "generate_feedback_with_llm": False,
        }
        return self._store_entry(entry, session_id)

    def _store_entry(self, entry: dict, session_id: str):
        if not session_id:
            return None
        return self._post_json(
            "/api/v1/remember/entry",
            {
                "entry": entry,
                "dataset_name": self.settings.dataset,
                "session_id": session_id,
            },
            self.settings.store_timeout,
        )

    # -- durable remember (explicit tool) -----------------------------------------

    def remember(self, content: str, *, node_set: str = "band_memory"):
        """Durably store content via ``/remember`` (server-side ingest + graph
        build, backgrounded so the call returns as soon as it is enqueued)."""
        url = self.settings.base_url.rstrip("/") + "/api/v1/remember"
        body, boundary = _multipart_body(
            {
                "datasetName": self.settings.dataset,
                "node_set": node_set,
                "run_in_background": "true",
            },
            [("data", f"{node_set}.txt", content.encode("utf-8"))],
        )
        req = urllib.request.Request(
            url,
            data=body,
            headers=self._headers(f"multipart/form-data; boundary={boundary}"),
            method="POST",
        )
        try:
            with self._open(req, timeout=self.settings.store_timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            return _error(e.code, f"server returned HTTP {e.code} for /api/v1/remember")
        except Exception as e:
            sys.stderr.write(
                f"[cognee-band] server unreachable at {self.settings.base_url}: {str(e)[:160]}\n"
            )
            return UNREACHABLE
        return {"ok": True}

    # -- session -> graph bridge ---------------------------------------------------

    def improve(self, session_id: str):
        """Bridge one session into the graph via ``/improve``. The server reads
        its own session cache, so no session text is sent. A 2xx submit counts
        as success (improve is idempotent server-side)."""
        if not session_id:
            return {"ok": False, "error": "missing session"}
        result = self._post_json(
            "/api/v1/improve",
            {
                "dataset_name": self.settings.dataset,
                "session_ids": [session_id],
                "run_in_background": True,
            },
            self.settings.improve_timeout,
        )
        if result == UNREACHABLE:
            return {"ok": False, "error": "unreachable"}
        if isinstance(result, dict) and result.get("error"):
            return {"ok": False, **result}
        return {"ok": True, "result": result if isinstance(result, dict) else {}}


def _multipart_body(fields, files):
    boundary = f"----cogneeBand{uuid.uuid4().hex}"
    chunks = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode())
        chunks.append(b"\r\n")
    for field_name, filename, content in files:
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            ).encode()
        )
        chunks.append(content if isinstance(content, bytes) else content.encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary
