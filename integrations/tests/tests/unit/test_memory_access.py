"""Exercise access management without granting live permissions."""

from uuid import uuid4

import pytest


@pytest.fixture
def access(suite, hook_module, monkeypatch):
    module = hook_module(suite, "memory-access.py")
    pc = module.pc
    monkeypatch.setenv("COGNEE_API_KEY", "owner-key")
    return module, pc


def test_grant_and_revoke_use_owner_key_and_explicit_ids(access, monkeypatch):
    module, pc = access
    principal, dataset = str(uuid4()), str(uuid4())
    calls = []

    def request(path, payload=None, **kwargs):
        calls.append((path, payload, kwargs))
        return {"id": "owner"} if path.endswith("/users/me") else {"ok": True}

    monkeypatch.setattr(pc, "_json_http_request", request)
    module.change_permission(principal, [dataset], "read")
    assert calls[-1][0].endswith(f"{principal}?permission_name=read")
    assert calls[-1][1] == [dataset]
    assert calls[-1][2]["api_key"] == "owner-key"
    module.change_permission(principal, [dataset], "write", revoke=True)
    assert calls[-1][2]["method"] == "DELETE"


def test_agent_credential_cannot_manage_permissions(access, monkeypatch):
    module, pc = access
    monkeypatch.setattr(
        pc, "_json_http_request", lambda *a, **kw: {"id": "agent", "parent_user_id": "owner"}
    )
    with pytest.raises(RuntimeError, match="principal"):
        module.change_permission(str(uuid4()), [str(uuid4())], "read")


def test_read_scope_is_bound_to_launch_and_identity(access, monkeypatch):
    module, pc = access
    ident = str(uuid4())
    pc.ensure_launch_record("host", "/tmp", dataset="write-dataset")
    monkeypatch.setattr(pc, "_json_http_request", lambda *a, **kw: [{"id": ident}])
    module.set_read_scope("host", [ident])
    pc.set_session_key("host")
    assert pc.load_graph_read_scope() == [ident]
    monkeypatch.setenv("COGNEE_API_KEY", "different-owner")
    with pytest.raises(RuntimeError, match="different identity"):
        pc.load_graph_read_scope()


def test_denied_read_selection_does_not_change_scope(access, monkeypatch):
    module, pc = access
    pc.ensure_launch_record("host", "/tmp", dataset="write-dataset")
    monkeypatch.setattr(pc, "_json_http_request", lambda *a, **kw: [])
    with pytest.raises(RuntimeError, match="grant access"):
        module.set_read_scope("host", [str(uuid4())])
    assert not pc.graph_read_scope_path("host").exists()


def test_connect_refuses_another_users_agent(access, monkeypatch):
    module, pc = access

    def request(path, **kwargs):
        return (
            {"id": "owner"}
            if kwargs["api_key"] == "owner-key"
            else {"id": "agent", "parent_user_id": "other-owner"}
        )

    monkeypatch.setattr(pc, "_json_http_request", request)
    with pytest.raises(RuntimeError, match="does not belong"):
        module.connect_existing_identity("agent-key")
    assert not pc._AGENT_KEY_CACHE.exists()
