"""Lightweight mock Cognee HTTP server built on pytest-httpserver.

Wraps a running ``HTTPServer`` and registers every endpoint the claude-code /
codex hooks call, backed by the stateful ``IdentityFake`` for the identity flow.
Stateless endpoints (health, remember, recall, improve, ...) return sensible,
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

#: Terminal cognify status the clients poll for on /api/v1/datasets/status.
STATUS_COMPLETED = "DATASET_PROCESSING_COMPLETED"
STATUS_ERRORED = "DATASET_PROCESSING_ERRORED"
STATUS_PROCESSING = "DATASET_PROCESSING_STARTED"

#: Stable ids for the default dataset/data the forget surface serves.
DEFAULT_DATASET_ID = "11111111-1111-5111-8111-111111111111"
DEFAULT_DATA_ID = "22222222-2222-5222-8222-222222222222"


def _json(status: int, body: Any) -> Response:
    return Response(json.dumps(body), status=status, content_type="application/json")


def _norm_path(path: str) -> str:
    """Canonical request path: trailing slash dropped (except for the root)."""
    return path.rstrip("/") or "/"


class MockCogneeServer:
    """Registers Cognee routes on a pytest-httpserver ``HTTPServer``.

    Construct with an already-started server; call ``.url`` for the base URL to
    inject as COGNEE_BASE_URL. Use ``.identity`` to seed identity branches,
    ``.set_recall_results`` / ``.set_dataset_status`` / ``.set_improve_response``
    / ``.set_credits_overview`` / ``.set_health_status`` to configure responses,
    and ``.assert_called`` / ``.calls`` to inspect traffic.
    """

    def __init__(self, httpserver, identity: IdentityFake | None = None) -> None:
        self.server = httpserver
        self.identity = identity or IdentityFake()
        self._recall_results: list[Any] = []
        self._health_status = 200
        # Cognify/memify status reported for every dataset id: a status string,
        # or a list walked one entry per poll (see set_dataset_status).
        self._dataset_status: Any = STATUS_COMPLETED
        self._status_polls = 0
        # None -> default: {"dataset_id": "ds-<dataset_name>", "status": "submitted"}.
        # {} is meaningful: the client treats it as "improve lock busy, retry".
        self._improve_response: dict | None = None
        self._credits_overview: Any = {"tenants": []}
        self._credits_fetches = 0
        # Forget surface: dataset listing, per-dataset data, raw content.
        # Named `_static_datasets`, NOT `_datasets_list`: that is the GET handler
        # below, and an instance attribute of the same name shadows the method —
        # `route(..., self._datasets_list)` then registers a list, every GET
        # /api/v1/datasets 500s with "'list' object is not callable", and the
        # merge of the forget skill with dataset switching did exactly that.
        self._static_datasets: list[dict] = [
            {"id": DEFAULT_DATASET_ID, "name": "agent_sessions", "createdAt": "2026-01-01T00:00:00"}
        ]
        self._dataset_data: list[dict] = [
            {"id": DEFAULT_DATA_ID, "name": "session_doc", "datasetId": DEFAULT_DATASET_ID}
        ]
        self._raw_content = "Session ID: test_session\n\nraw stored text"
        # (method, path) -> (status, body): short-circuits the normal handler.
        self._forced: dict[tuple[str, str], tuple[int, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self._register_routes()

    # -- public surface ----------------------------------------------------
    @property
    def url(self) -> str:
        return self.server.url_for("").rstrip("/")

    def set_recall_results(self, results: list[Any]) -> None:
        """Configure the JSON array returned by POST /api/v1/recall."""
        self._recall_results = list(results)

    def set_health_status(self, status: int) -> None:
        """Configure GET /health (e.g. 503 for the not-ready / skip path)."""
        self._health_status = status

    def set_dataset_status(self, status: str | list) -> None:
        """Configure the pipeline status GET /api/v1/datasets/status reports.

        Use STATUS_COMPLETED (default) / STATUS_ERRORED / STATUS_PROCESSING.
        Anything non-terminal makes pollers run to their deadline — keep
        deadlines short in such tests.

        Pass a list to drive a sequence across successive polls (the last entry
        sticks once exhausted). An ``int`` entry answers with that HTTP status
        instead of a body, which is how a transient poll failure mid-sequence
        is simulated.
        """
        self._dataset_status = status

    def set_improve_response(self, body: dict | None) -> None:
        """Configure POST /api/v1/improve. ``{}`` simulates the busy/lock skip."""
        self._improve_response = body

    def set_datasets(self, datasets: list[dict]) -> None:
        """Configure the JSON array returned by GET /api/v1/datasets when the
        identity fake has no seeded datasets (see ``_datasets_list``)."""
        self._static_datasets = list(datasets)

    def set_dataset_data(self, data: list[dict]) -> None:
        """Configure the JSON array returned by GET /api/v1/datasets/<id>/data."""
        self._dataset_data = list(data)

    def set_raw_content(self, text: str) -> None:
        """Configure the body of GET /api/v1/datasets/<id>/data/<id>/raw."""
        self._raw_content = text

    def set_credits_overview(self, overview: dict | list) -> None:
        """Configure GET /api/v1/billing/credits/overview.

        Real shape: {"tenants": [{"tenantId", "remainingUsd", "spentUsd",
        "maxBudgetUsd"}, ...]} — tenantId must equal the connected tenant
        (identity.tenant_id) for the client to show anything.

        Pass a list to drive successive fetches (the last entry sticks), which
        is how balance movement between two refreshes is expressed. An ``int``
        entry answers with that HTTP status instead of a body.
        """
        self._credits_overview = overview

    def force_response(self, method: str, path: str, status: int, body: Any = None) -> None:
        """Force one route to answer (status, body), bypassing its handler.

        Use for error-branch tests (e.g. recall -> 401/503). Pass ``bytes`` to
        send the body verbatim (for malformed-JSON tests); anything else is
        JSON-encoded. The request is still recorded. Clear with
        ``clear_forced``.
        """
        self._forced[(method, _norm_path(path))] = (status, body if body is not None else {})

    def clear_forced(self, method: str | None = None, path: str | None = None) -> None:
        """Drop forced responses (all of them, or one method+path pair)."""
        if method is None and path is None:
            self._forced.clear()
        else:
            self._forced.pop((method, _norm_path(path)), None)

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
            # Trailing slash normalized: clients may send either spelling (see the
            # POST /api/v1/datasets route), tests key on the bare path.
            "path": _norm_path(req.path),
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
            def dispatch(req: Request, _handler=handler) -> Response:
                forced = self._forced.get((req.method, _norm_path(req.path)))
                if forced is not None:
                    self._record(req)
                    status, body = forced
                    if isinstance(body, bytes):
                        # Verbatim: lets a test serve a body that is NOT valid JSON.
                        return Response(body, status=status, content_type="application/json")
                    return _json(status, body)
                return _handler(req)

            s.expect_request(uri, method=method).respond_with_handler(dispatch)

        # health / reachability
        route("/health", "GET", self._health)
        route("/docs", "GET", self._docs)
        route(
            "/openapi.json",
            "GET",
            lambda req: _json(
                200,
                {
                    "paths": {
                        "/api/v1/integrations/plugins/{plugin_key}/provision": {
                            "post": {"parameters": [{"name": "create_only", "in": "query"}]}
                        }
                    }
                }
                if self.identity.plugin_provisioning
                else {"paths": {}},
            ),
        )

        # auth + identity (single-principal-key flow)
        route("/api/v1/auth/login", "POST", self._login)
        route("/api/v1/auth/api-keys", "GET", self._list_api_keys)
        route("/api/v1/auth/api-keys", "POST", self._create_api_key)
        route("/api/v1/users/me", "GET", self._users_me)

        # agent session lifecycle
        route("/api/v1/agents/register", "POST", self._agents_register)
        route("/api/v1/agents/unregister", "POST", self._agents_unregister)
        route("/api/v1/agents/connections/me", "GET", self._agents_connections_me)

        # plugin identity provisioning (per-plugin agent sub-user + key).
        # Exact-path routing, so each known plugin key gets its own route.
        for plugin_key in ("claude-code", "codex"):
            route(
                f"/api/v1/integrations/plugins/{plugin_key}/provision",
                "POST",
                self._plugins_provision,
            )

        # memory
        route("/api/v1/remember", "POST", self._remember)
        route("/api/v1/remember/entry", "POST", self._remember_entry)
        route("/api/v1/recall", "POST", self._recall)
        route("/api/v1/improve", "POST", self._improve)
        # POST accepts both spellings: the clients send the trailing slash because
        # cloud tenants 307-redirect the bare path, and urllib will not replay a
        # POST across a 307.
        route(re.compile(r"^/api/v1/datasets/?$"), "POST", self._datasets)
        route("/api/v1/datasets", "GET", self._datasets_list)
        route("/api/v1/datasets/", "GET", self._datasets_list)
        route(
            re.compile(r"/api/v1/permissions/principals/[^/]+/datasets"),
            "GET",
            lambda req: _json(
                200,
                [
                    row
                    for row in self.identity.datasets.values()
                    if row.get("ownerId") == req.path.split("/")[-2]
                ],
            ),
        )
        route("/api/v1/datasets/status", "GET", self._datasets_status)

        # forget surface (dataset inspection + deletion); the listing itself is
        # the shared GET /api/v1/datasets route above.
        route(re.compile(r"^/api/v1/datasets/[^/]+/data$"), "GET", self._dataset_data_get)
        route(re.compile(r"^/api/v1/datasets/[^/]+/data/[^/]+/raw$"), "GET", self._dataset_raw_get)
        route("/api/v1/forget", "POST", self._forget)

        # cloud platform (billing)
        route("/api/v1/billing/credits/overview", "GET", self._credits)

    # -- handlers ----------------------------------------------------------
    def _health(self, req: Request) -> Response:
        self._record(req)
        status = self._health_status
        return _json(status, {"status": "ok" if status < 400 else "unavailable"})

    def _docs(self, req: Request) -> Response:
        self._record(req)
        return Response("ok", status=200)

    def _login(self, req: Request) -> Response:
        self._record(req)
        status, body = self.identity.login(
            req.form.get("username", ""), req.form.get("password", "")
        )
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

    def _agents_register(self, req: Request) -> Response:
        self._record(req)
        status, body = self.identity.agents_register(req.get_json(silent=True) or {})
        return _json(status, body)

    def _agents_unregister(self, req: Request) -> Response:
        self._record(req)
        status, body = self.identity.agents_unregister(req.get_json(silent=True) or {})
        return _json(status, body)

    def _agents_connections_me(self, req: Request) -> Response:
        self._record(req)
        status, body = self.identity.agents_connections_me(req.args.get("agent_session_name"))
        return _json(status, body)

    def _plugins_provision(self, req: Request) -> Response:
        self._record(req)
        # /api/v1/integrations/plugins/{plugin_key}/provision
        plugin_key = req.path.rstrip("/").split("/")[-2]
        status, body = self.identity.plugins_provision(
            plugin_key,
            req.headers.get("X-Api-Key"),
            create_only=req.args.get("create_only") == "true",
        )
        return _json(status, body)

    def _remember(self, req: Request) -> Response:
        self._record(req)
        # Background remember returns an enqueue handle the client may poll
        # via /api/v1/datasets/status.
        dataset = req.form.get("datasetName", "")
        _, ds = self.identity.datasets_create(dataset or "default")
        return _json(
            200,
            {"dataset_id": ds["id"], "pipeline_run_id": f"run-{len(self.calls)}"},
        )

    def _remember_entry(self, req: Request) -> Response:
        self._record(req)
        return _json(200, {"entry_id": f"entry-{len(self.calls)}"})

    def _recall(self, req: Request) -> Response:
        self._record(req)
        # Response MUST be a top-level JSON array (both clients expect a list).
        return _json(200, self._recall_results)

    def _improve(self, req: Request) -> Response:
        self._record(req)
        if self._improve_response is not None:
            return _json(200, self._improve_response)
        body_in = req.get_json(silent=True) or {}
        dataset = str(body_in.get("dataset_name") or "default")
        _, ds = self.identity.datasets_create(dataset)
        return _json(200, {"dataset_id": ds["id"], "status": "submitted"})

    def _datasets(self, req: Request) -> Response:
        self._record(req)
        body_in = req.get_json(silent=True) or {}
        status, body = self.identity.datasets_create(body_in.get("name", "default"))
        return _json(status, body)

    def _datasets_list(self, req: Request) -> Response:
        """GET /api/v1/datasets — one route, two kinds of test behind it.

        Tests that model ownership (dataset switching) seed the identity fake and
        get exactly what they seeded, camelCase like the real OutDTO (``ownerId``).
        Tests that only need *a* listing (the forget skill's ``datasets`` command)
        seed nothing and get the fixed ``set_datasets`` list instead.
        """
        self._record(req)
        if self.identity.datasets:
            status, body = self.identity.datasets_list()
            return _json(status, body)
        return _json(200, self._static_datasets)

    def _dataset_data_get(self, req: Request) -> Response:
        self._record(req)
        return _json(200, self._dataset_data)

    def _dataset_raw_get(self, req: Request) -> Response:
        self._record(req)
        return Response(self._raw_content, status=200, content_type="text/plain")

    def _forget(self, req: Request) -> Response:
        self._record(req)
        return _json(200, {"status": "success"})

    def _datasets_status(self, req: Request) -> Response:
        self._record(req)
        dataset_id = req.args.get("dataset", "")
        status = self._dataset_status
        if isinstance(status, list):
            # Walk the sequence once per poll; the final entry sticks.
            index = min(self._status_polls, len(status) - 1) if status else 0
            self._status_polls += 1
            status = status[index] if status else STATUS_COMPLETED
        if isinstance(status, int):
            # An HTTP status instead of a body: a transient poll failure.
            return _json(status, {"detail": f"status poll failed ({status})"})
        # Clients accept {<dataset_id>: "<STATUS>"} (or a nested per-pipeline
        # dict; the flat string form serves every pipeline).
        return _json(200, {dataset_id: status})

    def _credits(self, req: Request) -> Response:
        self._record(req)
        overview = self._credits_overview
        if isinstance(overview, list):
            index = min(self._credits_fetches, len(overview) - 1) if overview else 0
            self._credits_fetches += 1
            overview = overview[index] if overview else {"tenants": []}
        if isinstance(overview, int):
            return _json(overview, {"detail": f"billing fetch failed ({overview})"})
        return _json(200, overview)
