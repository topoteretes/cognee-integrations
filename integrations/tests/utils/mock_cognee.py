"""Lightweight mock Cognee HTTP server built on pytest-httpserver.

Wraps a running ``HTTPServer`` and registers every endpoint the claude-code /
codex hooks call, backed by the stateful ``IdentityFake`` for the identity flow.
Stateless endpoints (health, remember, recall, remember/entry) return sensible,
per-test-overridable responses.

Request shapes are matched leniently — assertions check only the fields a test
cares about (``assert_called``), never a deep-equal of the whole body.
"""

from __future__ import annotations

import json
import re
from typing import Any

from werkzeug import Request, Response

from .identity_fake import IdentityFake

_AGENT_ID_PATH = re.compile(r"^/api/v1/agents/[^/]+$")


def _json(status: int, body: Any) -> Response:
    return Response(json.dumps(body), status=status, content_type="application/json")


class MockCogneeServer:
    """Registers Cognee routes on a pytest-httpserver ``HTTPServer``.

    Construct with an already-started server; call ``.url`` for the base URL to
    inject as COGNEE_SERVICE_URL. Use ``.identity`` to seed identity branches,
    ``.set_recall_results`` to configure recall, and ``.assert_called`` /
    ``.calls`` to inspect traffic.
    """

    def __init__(self, httpserver, identity: IdentityFake | None = None) -> None:
        self.server = httpserver
        self.identity = identity or IdentityFake()
        self._recall_results: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self._register_routes()

    # -- public surface ----------------------------------------------------
    @property
    def url(self) -> str:
        return self.server.url_for("").rstrip("/")

    def set_recall_results(self, results: list[Any]) -> None:
        """Configure the JSON array returned by POST /api/v1/recall."""
        self._recall_results = list(results)

    def assert_called(self, method: str, path: str, **json_fields: Any) -> dict[str, Any]:
        """Assert a matching request was recorded; return the call entry.

        ``json_fields`` are matched against the recorded JSON body (subset match).
        """
        for entry in self.calls:
            if entry["method"] != method or entry["path"] != path:
                continue
            if not json_fields:
                return entry
            body = entry.get("json") or {}
            if all(body.get(k) == v for k, v in json_fields.items()):
                return entry
        raise AssertionError(
            f"No recorded request matched {method} {path} {json_fields}. "
            f"Recorded: {[(c['method'], c['path']) for c in self.calls]}"
        )

    def assert_not_called(self, method: str, path: str) -> None:
        for entry in self.calls:
            if entry["method"] == method and entry["path"] == path:
                raise AssertionError(f"Unexpected request {method} {path}")

    # -- recording ---------------------------------------------------------
    def _record(self, req: Request) -> None:
        entry: dict[str, Any] = {
            "method": req.method,
            "path": req.path,
            "query": dict(req.args),
            "headers": dict(req.headers),
        }
        ctype = req.headers.get("Content-Type", "")
        try:
            if "application/json" in ctype:
                entry["json"] = req.get_json(silent=True)
            elif "multipart/form-data" in ctype:
                entry["form"] = dict(req.form)
                entry["files"] = list(req.files.keys())
            elif "application/x-www-form-urlencoded" in ctype:
                entry["form"] = dict(req.form)
        except Exception:  # pragma: no cover - defensive
            pass
        self.calls.append(entry)

    # -- route registration ------------------------------------------------
    def _register_routes(self) -> None:
        s = self.server

        def route(uri, method, handler):
            s.expect_request(uri, method=method).respond_with_handler(handler)

        # health / reachability
        route("/health", "GET", self._health)
        route("/docs", "GET", self._docs)

        # auth + identity
        route("/api/v1/auth/login", "POST", self._login)
        route("/api/v1/auth/register", "POST", self._register)
        route("/api/v1/auth/api-keys", "GET", self._list_api_keys)
        route("/api/v1/auth/api-keys", "POST", self._create_api_key)
        route("/api/v1/users/me", "GET", self._users_me)

        # agents
        route("/api/v1/agents/create", "POST", self._agents_create)
        route("/api/v1/agents/list", "GET", self._agents_list)
        route(_AGENT_ID_PATH, "DELETE", self._agents_delete)
        route("/api/v1/agents/register", "POST", self._agents_register)
        route("/api/v1/agents/unregister", "POST", self._agents_unregister)
        route("/api/v1/agents/connections/me", "GET", self._agents_connections_me)

        # memory
        route("/api/v1/remember", "POST", self._remember)
        route("/api/v1/remember/entry", "POST", self._remember_entry)
        route("/api/v1/recall", "POST", self._recall)
        route("/api/v1/datasets", "POST", self._datasets)

    # -- handlers ----------------------------------------------------------
    def _health(self, req: Request) -> Response:
        self._record(req)
        return _json(200, {"status": "ok"})

    def _docs(self, req: Request) -> Response:
        self._record(req)
        return Response("ok", status=200)

    def _login(self, req: Request) -> Response:
        self._record(req)
        status, body = self.identity.login(
            req.form.get("username", ""), req.form.get("password", "")
        )
        return _json(status, body)

    def _register(self, req: Request) -> Response:
        self._record(req)
        body_in = req.get_json(silent=True) or {}
        status, body = self.identity.register(body_in.get("email", ""))
        return _json(status, body)

    def _list_api_keys(self, req: Request) -> Response:
        self._record(req)
        status, body = self.identity.list_api_keys(req.cookies.get("auth_token"))
        return _json(status, body)

    def _create_api_key(self, req: Request) -> Response:
        self._record(req)
        status, body = self.identity.create_api_key(req.cookies.get("auth_token"))
        return _json(status, body)

    def _users_me(self, req: Request) -> Response:
        self._record(req)
        status, body = self.identity.users_me(req.headers.get("X-Api-Key"))
        return _json(status, body)

    def _agents_create(self, req: Request) -> Response:
        self._record(req)
        status, body = self.identity.agents_create(req.args.get("name", ""))
        return _json(status, body)

    def _agents_list(self, req: Request) -> Response:
        self._record(req)
        status, body = self.identity.agents_list()
        return _json(status, body)

    def _agents_delete(self, req: Request) -> Response:
        self._record(req)
        agent_id = req.path.rsplit("/", 1)[-1]
        status, body = self.identity.agents_delete(agent_id)
        return _json(status, body)

    def _agents_register(self, req: Request) -> Response:
        self._record(req)
        status, body = self.identity.agents_register()
        return _json(status, body)

    def _agents_unregister(self, req: Request) -> Response:
        self._record(req)
        status, body = self.identity.agents_unregister()
        return _json(status, body)

    def _agents_connections_me(self, req: Request) -> Response:
        self._record(req)
        status, body = self.identity.agents_connections_me()
        return _json(status, body)

    def _remember(self, req: Request) -> Response:
        self._record(req)
        dataset = req.form.get("datasetName", "")
        return _json(
            200,
            {"dataset_id": f"ds-{dataset or 'default'}", "dataset_name": dataset, "status": "ok"},
        )

    def _remember_entry(self, req: Request) -> Response:
        self._record(req)
        return _json(200, {"entry_id": f"entry-{len(self.calls)}"})

    def _recall(self, req: Request) -> Response:
        self._record(req)
        # Response MUST be a top-level JSON array (both clients expect a list).
        return _json(200, self._recall_results)

    def _datasets(self, req: Request) -> Response:
        self._record(req)
        body_in = req.get_json(silent=True) or {}
        status, body = self.identity.datasets_create(body_in.get("name", "default"))
        return _json(status, body)
