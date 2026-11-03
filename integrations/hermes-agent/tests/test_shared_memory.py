"""Shared agent memory for the provisioned ``hermes-agent`` identity (SDK-646).

Under ``COGNEE_PLUGIN_IDENTITY=auto`` (the default) the plugin provisions its
own agent only in service of shared agent memory: the agent joins the user's
tenant and ``cognee-agent`` role, the role is granted the user's datasets, and
the session's dataset is addressed by the UUID of a canonical, parent-owned
copy. When that wiring cannot be completed the freshly minted identity is
reverted — its key revoked, the cache cleared — so the session runs as the
principal and nothing the user owns is ever stranded behind an agent that
cannot reach it. Mirrors ``ensure_shared_memory`` in the Claude Code plugin.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest
from cognee_integration_hermes import exit_watcher, plugin_identity, shared_memory
from cognee_integration_hermes.http_backend import HttpBackend
from cognee_integration_hermes.plugin_identity import PLUGIN_KEY, PROVISION_PATH
from test_http_backend import FakeOpener, _Response

_URL = "http://127.0.0.1:8011"
PRINCIPAL = "principal-key"
AGENT_KEY = "agentkey-1"
AGENT_ID = "agent-uuid-1"
PARENT_ID = "parent-uuid-1"
DATASET = "agent_sessions"

PROVISIONED = {"agentId": AGENT_ID, "apiKey": AGENT_KEY, "keyId": "k1", "created": True}
OPENAPI_ADDITIVE = {"paths": {PROVISION_PATH: {"post": {"x-cognee-additive-plugin-keys": True}}}}
KEYS_PATH = f"/api/v1/integrations/plugins/{PLUGIN_KEY}/keys/k1"
PARENT_DS = {
    "id": "ds-1",
    "name": DATASET,
    "owner_id": PARENT_ID,
    "created_at": "2026-01-01T00:00:00Z",
}
AGENT_DS = {
    "id": "ds-2",
    "name": DATASET,
    "owner_id": AGENT_ID,
    "created_at": "2026-02-01T00:00:00Z",
}


def _http_error(path, status, detail="nope"):
    return urllib.error.HTTPError(
        _URL + path, status, "err", {}, io.BytesIO(json.dumps({"detail": detail}).encode())
    )


class Seq(list):
    """A queue of responses for one route, consumed in order; the last one repeats."""


class RouteOpener(FakeOpener):
    """Responses keyed by ``"METHOD /path"`` (query dropped); a ``Seq`` is consumed in order."""

    def __call__(self, request, timeout=None):
        path = request.selector if hasattr(request, "selector") else request.full_url
        self.requests.append(
            {
                "method": request.get_method(),
                "path": path,
                "url": request.full_url,
                "headers": {k.lower(): v for k, v in request.header_items()},
                "body": request.data,
                "timeout": timeout,
            }
        )
        key = f"{request.get_method()} {self._key(path)}"
        outcome = self.responses.get(key, self.responses.get(self._key(path), self.default))
        if isinstance(outcome, Seq):
            outcome = outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(outcome)

    def calls(self, method, path):
        return [r for r in self.requests if r["method"] == method and self._key(r["path"]) == path]

    def call(self, method, path):
        matches = self.calls(method, path)
        assert len(matches) == 1, f"expected one {method} {path}, got {len(matches)}"
        return matches[0]

    def body(self, method, path):
        return json.loads(self.call(method, path)["body"].decode("utf-8"))


def _routes(**overrides):
    routes = {
        "GET /health": {"status": "ok"},
        "GET /openapi.json": OPENAPI_ADDITIVE,
        f"POST {PROVISION_PATH}": PROVISIONED,
        "POST /api/v1/agents/register": {"registered": True},
        "POST /api/v1/agents/unregister": {"activeAgents": 0},
        "GET /api/v1/permissions/tenants/me": {},
        "GET /api/v1/users/me": {"id": PARENT_ID, "tenant_id": ""},
        # Fresh install: nothing to read until the canonical dataset is created.
        "GET /api/v1/datasets": Seq([[], [], [PARENT_DS]]),
        "POST /api/v1/permissions/tenants": {"tenant_id": "t-1"},
        "GET /api/v1/permissions/tenants/t-1/roles": [],
        "POST /api/v1/permissions/roles": {"role_id": "r-1"},
        f"POST /api/v1/permissions/users/{AGENT_ID}/tenants": {},
        "POST /api/v1/permissions/tenants/select": {},
        f"POST /api/v1/permissions/users/{AGENT_ID}/roles": {},
        "POST /api/v1/datasets": {"id": "ds-1", "name": DATASET},
        "POST /api/v1/permissions/datasets/r-1": {},
        "POST /api/v1/recall": [],
        "POST /api/v1/remember": {"status": "running"},
        "POST /api/v1/improve": {"status": "ok"},
        "POST /api/v1/forget": {"ok": True},
        f"DELETE {KEYS_PATH}": {},
    }
    routes.update(overrides)
    return RouteOpener(routes)


@pytest.fixture
def state(monkeypatch, tmp_path):
    cache = tmp_path / PLUGIN_KEY / "agent_key.json"
    marker = tmp_path / PLUGIN_KEY / "shared_memory.json"
    monkeypatch.setattr(plugin_identity, "AGENT_KEY_CACHE", cache)
    monkeypatch.setattr(shared_memory, "SHARED_MEMORY_MARKER", marker)
    monkeypatch.setattr(shared_memory, "plugin_version", lambda: "test-version")
    return {"cache": cache, "marker": marker}


def _connect(opener, mode="auto", api_key=PRINCIPAL):
    backend = HttpBackend(opener=opener)
    backend.connect(url=_URL, api_key=api_key, timeout=5, plugin_identity=mode)
    return backend


def _marker(state):
    return json.loads(state["marker"].read_text())


# -- auto: provision for shared memory, wire, address by UUID --------------------


class TestAutoWiring:
    def test_fresh_install_provisions_wires_and_addresses_the_canonical_dataset(self, state):
        opener = _routes()
        backend = _connect(opener)

        # Provisioned as the principal, running as the agent.
        assert opener.call("POST", PROVISION_PATH)["headers"]["x-api-key"] == PRINCIPAL
        assert backend.api_key == AGENT_KEY and backend.agent_id == AGENT_ID

        # Wired: tenant created (tenant-less parent, no datasets), role created,
        # agent made a member — the control plane as the PRINCIPAL, the tenant
        # selection as the AGENT itself.
        assert (
            opener.call("POST", "/api/v1/permissions/tenants")["headers"]["x-api-key"] == PRINCIPAL
        )
        assert (
            "tenant_name=cognee-parent-u"
            in opener.call("POST", "/api/v1/permissions/tenants")["path"]
        )
        assert opener.call("POST", "/api/v1/permissions/roles")["headers"]["x-api-key"] == PRINCIPAL
        membership = opener.call("POST", f"/api/v1/permissions/users/{AGENT_ID}/tenants")
        assert (
            membership["headers"]["x-api-key"] == PRINCIPAL
            and "tenant_id=t-1" in membership["path"]
        )
        select = opener.call("POST", "/api/v1/permissions/tenants/select")
        assert select["headers"]["x-api-key"] == AGENT_KEY
        assert json.loads(select["body"]) == {"tenant_id": "t-1"}
        assert (
            "role_id=r-1"
            in opener.call("POST", f"/api/v1/permissions/users/{AGENT_ID}/roles")["path"]
        )
        assert backend.shared["mode"] == "shared"
        assert _marker(state) == {
            **_marker(state),
            "mode": "shared",
            "tenant_id": "t-1",
            "role_id": "r-1",
            "parent_user_id": PARENT_ID,
            "agent_id": AGENT_ID,
            "plugin_version": "test-version",
        }
        # Registered as the agent, naming the plugin.
        register = opener.call("POST", "/api/v1/agents/register")
        assert register["headers"]["x-api-key"] == AGENT_KEY
        assert json.loads(register["body"])["metadata"] == {"plugin_key": PLUGIN_KEY}

        # The session's dataset: created as the PRINCIPAL (never as the agent),
        # granted read+write to the role, addressed by UUID from then on.
        backend.ensure_dataset(dataset=DATASET, timeout=5)
        create = opener.call("POST", "/api/v1/datasets")
        assert create["headers"]["x-api-key"] == PRINCIPAL
        grants = opener.calls("POST", "/api/v1/permissions/datasets/r-1")
        assert sorted(g["path"].split("permission_name=")[1] for g in grants) == ["read", "write"]
        assert all(json.loads(g["body"]) == ["ds-1"] for g in grants)
        assert backend.dataset_ids_for(DATASET) == ("ds-1", ["ds-1"])
        assert _marker(state)["canonical"] == {DATASET: "ds-1"}
        assert _marker(state)["granted"] == {"ds-1": "ok"}

        # Data plane: UUIDs, as the agent.
        backend.recall(
            query="q",
            session_id=None,
            datasets=[DATASET],
            top_k=3,
            auto_route=True,
            query_type=None,
            timeout=5,
        )
        recall = opener.body("POST", "/api/v1/recall")
        assert recall["dataset_ids"] == ["ds-1"] and "datasets" not in recall
        assert opener.call("POST", "/api/v1/recall")["headers"]["x-api-key"] == AGENT_KEY
        backend.remember_permanent(text="hello", dataset=DATASET, session_ids=[], timeout=5)
        fields = opener.multipart_fields("/api/v1/remember")
        assert fields["datasetId"] == "ds-1" and "datasetName" not in fields
        backend.improve(dataset=DATASET, session_ids=["s1"], background=False, timeout=5)
        assert opener.body("POST", "/api/v1/improve") == {
            "dataset_name": DATASET,
            "dataset_id": "ds-1",
            "session_ids": ["s1"],
            "run_in_background": False,
        }
        backend.forget(dataset=DATASET, everything=False, memory_only=False, timeout=5)
        assert opener.body("POST", "/api/v1/forget")["dataset_id"] == "ds-1"
        # Typed session entries keep the name: the server records only the
        # session's dataset from them, and improve bridges by UUID (SDK-1002).
        backend.remember_entry(entry={"type": "qa"}, session_id="s1", dataset=DATASET, timeout=5)
        assert opener.body("POST", "/api/v1/remember/entry")["dataset_name"] == DATASET
        # The exit watcher closes the session as the same identity.
        assert backend.connection_info()["api_key"] == AGENT_KEY

    def test_restart_reuses_the_wiring_without_setting_it_up_again(self, state):
        _connect(_routes()).ensure_dataset(dataset=DATASET, timeout=5)
        opener = _routes(**{"GET /api/v1/datasets": [PARENT_DS]})
        backend = _connect(opener)
        backend.ensure_dataset(dataset=DATASET, timeout=5)
        assert backend.api_key == AGENT_KEY
        assert opener.calls("POST", PROVISION_PATH) == []
        assert opener.calls("POST", "/api/v1/permissions/tenants") == []
        assert opener.calls("POST", "/api/v1/permissions/roles") == []
        assert opener.calls("POST", "/api/v1/datasets") == []  # canonical already exists
        assert opener.calls("POST", "/api/v1/permissions/datasets/r-1") == []  # grant memoised
        assert backend.dataset_ids_for(DATASET) == ("ds-1", ["ds-1"])

    def test_parent_owned_copy_is_canonical_and_the_agents_own_copy_is_read(self, state):
        opener = _routes(
            **{
                "GET /api/v1/users/me": {"id": PARENT_ID, "tenant_id": "t-1"},
                "GET /api/v1/datasets": [PARENT_DS, AGENT_DS],
                "GET /api/v1/permissions/tenants/t-1/roles": [
                    {"id": "r-1", "name": "cognee-agent"}
                ],
                # The agent's own copy cannot be granted by the parent (403) — it
                # is still readable, because the agent owns it.
                "POST /api/v1/permissions/datasets/r-1": Seq(
                    [{}, {}, _http_error("/x", 403), _http_error("/x", 403)]
                ),
            }
        )
        backend = _connect(opener)
        backend.ensure_dataset(dataset=DATASET, timeout=5)
        assert opener.calls("POST", "/api/v1/permissions/tenants") == []  # the parent has one
        assert opener.calls("POST", "/api/v1/permissions/roles") == []  # the role exists
        assert opener.calls("POST", "/api/v1/datasets") == []
        assert backend.dataset_ids_for(DATASET) == ("ds-1", ["ds-1", "ds-2"])
        backend.recall(
            query="q",
            session_id=None,
            datasets=[DATASET],
            top_k=3,
            auto_route=True,
            query_type=None,
            timeout=5,
        )
        assert opener.body("POST", "/api/v1/recall")["dataset_ids"] == ["ds-1", "ds-2"]

    def test_a_second_dataset_resolves_its_own_canonical_copy(self, state):
        opener = _routes(
            **{
                "GET /api/v1/datasets": Seq([[], [], [PARENT_DS], [PARENT_DS]]),
                "POST /api/v1/datasets": Seq(
                    [{"id": "ds-1", "name": DATASET}, {"id": "ds-9", "name": "notes"}]
                ),
            }
        )
        backend = _connect(opener)
        backend.ensure_dataset(dataset=DATASET, timeout=5)
        backend.ensure_dataset(dataset="notes", timeout=5)
        assert backend.dataset_ids_for("notes") == ("ds-9", ["ds-9"])
        # The previous dataset stays addressable by UUID (a retired session's bridge).
        assert backend.dataset_ids_for(DATASET) == ("ds-1", ["ds-1"])
        assert _marker(state)["canonical"] == {DATASET: "ds-1", "notes": "ds-9"}


# -- auto: revert when shared memory cannot be wired -----------------------------


class TestAutoRevert:
    def test_server_without_permissions_api_reverts_the_fresh_identity(self, state):
        opener = _routes(
            **{
                "GET /api/v1/permissions/tenants/me": _http_error(
                    "/api/v1/permissions/tenants/me", 404
                )
            }
        )
        backend = _connect(opener)
        assert opener.calls("POST", PROVISION_PATH)  # it did try
        revoke = opener.call("DELETE", KEYS_PATH)
        assert revoke["headers"]["x-api-key"] == PRINCIPAL  # only this machine's key, as the user
        assert backend.api_key == PRINCIPAL and not backend.identity_active
        assert not state["cache"].exists()
        assert _marker(state)["reason"] == "unsupported"
        assert opener.call("POST", "/api/v1/agents/register")["headers"]["x-api-key"] == PRINCIPAL
        # Name addressing from here on.
        backend.ensure_dataset(dataset=DATASET, timeout=5)
        assert opener.call("POST", "/api/v1/datasets")["headers"]["x-api-key"] == PRINCIPAL
        assert backend.dataset_ids_for(DATASET) == ("", [])

    def test_structural_failure_is_not_retried_on_the_same_plugin_version(self, state):
        bad = {"GET /api/v1/permissions/tenants/me": _http_error("/x", 404)}
        _connect(_routes(**bad))
        opener = _routes(**bad)
        _connect(opener)
        assert opener.calls("POST", PROVISION_PATH) == []
        assert opener.calls("GET", "/openapi.json") == []

    def test_structural_failure_is_retried_after_a_plugin_update(self, state, monkeypatch):
        bad = {"GET /api/v1/permissions/tenants/me": _http_error("/x", 404)}
        _connect(_routes(**bad))
        monkeypatch.setattr(shared_memory, "plugin_version", lambda: "newer-version")
        opener = _routes()
        backend = _connect(opener)
        assert opener.calls("POST", PROVISION_PATH)
        assert backend.shared["mode"] == "shared"

    def test_tenantless_parent_with_data_stays_on_the_principal(self, state):
        opener = _routes(**{"GET /api/v1/datasets": [PARENT_DS]})
        backend = _connect(opener)
        assert backend.api_key == PRINCIPAL
        assert _marker(state)["reason"] == "tenantless_with_data"
        assert opener.calls("POST", "/api/v1/permissions/tenants") == []
        assert opener.calls("DELETE", KEYS_PATH)

    def test_org_member_who_cannot_create_roles_stays_on_the_principal(self, state):
        opener = _routes(
            **{
                "GET /api/v1/users/me": {"id": PARENT_ID, "tenant_id": "t-shared"},
                "GET /api/v1/permissions/tenants/t-shared/roles": [],
                "POST /api/v1/permissions/roles": _http_error("/x", 403),
            }
        )
        backend = _connect(opener)
        assert backend.api_key == PRINCIPAL
        assert _marker(state)["reason"] == "not_tenant_owner"

    def test_shared_memory_opted_out_provisions_nothing(self, state, monkeypatch):
        monkeypatch.setenv("COGNEE_SHARED_AGENT_MEMORY", "false")
        opener = _routes()
        backend = _connect(opener)
        assert opener.calls("POST", PROVISION_PATH) == []
        assert backend.api_key == PRINCIPAL

    def test_config_value_opts_out_when_the_env_is_unset(self, state):
        opener = _routes()
        backend = HttpBackend(opener=opener)
        backend.connect(url=_URL, api_key=PRINCIPAL, timeout=5, shared_agent_memory="false")
        assert opener.calls("POST", PROVISION_PATH) == []
        assert backend.api_key == PRINCIPAL


# -- explicit identity: kept whatever shared memory does ---------------------------


class TestExplicitIdentity:
    def test_true_keeps_the_identity_when_shared_memory_is_unsupported(self, state):
        opener = _routes(**{"GET /api/v1/permissions/tenants/me": _http_error("/x", 404)})
        backend = _connect(opener, "true")
        assert backend.api_key == AGENT_KEY and backend.identity_active
        assert opener.calls("DELETE", KEYS_PATH) == []
        assert backend.shared["mode"] == "separated"
        # Separated: the agent's own copy, by name.
        backend.ensure_dataset(dataset=DATASET, timeout=5)
        assert opener.call("POST", "/api/v1/datasets")["headers"]["x-api-key"] == AGENT_KEY
        assert backend.dataset_ids_for(DATASET) == ("", [])

    def test_true_wires_shared_memory_when_available(self, state):
        backend = _connect(_routes(), "true")
        backend.ensure_dataset(dataset=DATASET, timeout=5)
        assert backend.identity_active and backend.shared["mode"] == "shared"
        assert backend.dataset_ids_for(DATASET) == ("ds-1", ["ds-1"])


# -- opt-out with an existing identity: leave the role, keep the agent --------------


class TestOptOut:
    def test_opting_out_leaves_the_role_and_addresses_by_name(self, state, monkeypatch):
        _connect(_routes()).ensure_dataset(dataset=DATASET, timeout=5)
        monkeypatch.setenv("COGNEE_SHARED_AGENT_MEMORY", "false")
        opener = _routes(**{f"DELETE /api/v1/permissions/users/{AGENT_ID}/roles": {}})
        backend = _connect(opener)
        leave = opener.call("DELETE", f"/api/v1/permissions/users/{AGENT_ID}/roles")
        assert leave["headers"]["x-api-key"] == PRINCIPAL and "role_id=r-1" in leave["path"]
        assert backend.api_key == AGENT_KEY  # the identity stays; its memory is its own now
        assert backend.shared == {**backend.shared, "mode": "separated", "reason": "opt_out"}
        assert _marker(state)["mode"] == "separated"
        assert _marker(state)["role_id"] == "r-1"  # kept, so re-enabling rejoins the same role
        assert backend.dataset_ids_for(DATASET) == ("", [])


# -- the exit watcher bridges by UUID too --------------------------------------------


def test_exit_watcher_bridges_the_canonical_dataset(monkeypatch):
    posts = []
    monkeypatch.setattr(
        exit_watcher, "_post", lambda url, path, payload, **kw: posts.append((path, payload))
    )
    monkeypatch.setenv("COGNEE_API_KEY", AGENT_KEY)
    exit_watcher.fire(
        {
            "url": _URL,
            "agent_session_name": "hermes",
            "dataset": DATASET,
            "dataset_id": "ds-1",
            "session_id": "s1",
            "improve": True,
            "improve_timeout": 5,
        }
    )
    assert posts[0][0] == "/api/v1/improve"
    assert posts[0][1] == {
        "dataset_name": DATASET,
        "dataset_id": "ds-1",
        "session_ids": ["s1"],
        "run_in_background": False,
    }


def test_exit_watcher_state_carries_the_dataset_id(monkeypatch, tmp_path):
    monkeypatch.setattr(exit_watcher, "_spawn", lambda *a, **k: None)
    state_path = tmp_path / "w.json"
    exit_watcher.arm(
        state_path=state_path,
        log_path=tmp_path / "w.log",
        parent_pid=1,
        url=_URL,
        api_key="k",
        agent_session_name="hermes",
        dataset=DATASET,
        dataset_id="ds-1",
        session_id="s1",
        improve=True,
        improve_timeout=5,
    )
    assert json.loads(state_path.read_text())["dataset_id"] == "ds-1"
    exit_watcher.finalize(
        state_path,
        api_key="k",
        session_id="s2",
        dataset="notes",
        dataset_id="ds-9",
        improve=True,
        improve_timeout=5,
    )
    assert json.loads(state_path.read_text())["dataset_id"] == "ds-9"
