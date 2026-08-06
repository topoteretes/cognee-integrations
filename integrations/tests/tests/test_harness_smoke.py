"""Harness self-tests: the mock server, identity fake, and isolation work.

These exercise the infrastructure itself (not the integrations), so later test
tasks can build on it with confidence. The e2e canary at the bottom runs a real
hook script against the mock server, once per suite.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
import uuid

from utils import config_dir, plugin_root, state_dir
from utils.identity_fake import make_jwt
from utils.isolation import DETERMINISTIC_ENV, build_env
from utils.mock_cognee import STATUS_ERRORED


def _http(url: str, *, method: str = "GET", data: bytes | None = None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read() or b"null")


def _post_json(url: str, payload: dict, headers=None):
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    return _http(url, method="POST", data=json.dumps(payload).encode(), headers=hdrs)


# -- mock server core endpoints ------------------------------------------------


def test_health_and_docs(mock_server):
    assert _http(f"{mock_server.url}/health")[0] == 200
    req = urllib.request.Request(f"{mock_server.url}/docs")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200

    mock_server.set_health_status(503)
    try:
        _http(f"{mock_server.url}/health")
        raise AssertionError("expected HTTP 503")
    except urllib.error.HTTPError as exc:
        assert exc.code == 503


def test_recall_returns_configured_array(mock_server):
    status, body = _post_json(
        f"{mock_server.url}/api/v1/recall",
        {"query": "q", "session_id": "s", "top_k": 3, "scope": ["session"]},
    )
    assert (status, body) == (200, [])

    mock_server.set_recall_results(["memory one", {"text": "memory two", "score": 0.9}])
    _, body = _post_json(f"{mock_server.url}/api/v1/recall", {"query": "q"})
    assert body[0] == "memory one" and body[1]["score"] == 0.9
    mock_server.assert_called("POST", "/api/v1/recall", query="q")


def test_remember_multipart_returns_enqueue_handle(mock_server):
    boundary = f"----test{uuid.uuid4().hex}"
    parts = []
    for name, value in [("datasetName", "agent_sessions"), ("run_in_background", "true")]:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="data"; filename="doc.txt"\r\n'
        "Content-Type: text/plain\r\n\r\nhello\r\n"
    )
    body = ("".join(parts) + f"--{boundary}--\r\n").encode()

    status, resp = _http(
        f"{mock_server.url}/api/v1/remember",
        method="POST",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert status == 200 and resp["dataset_id"] and resp["pipeline_run_id"]

    call = mock_server.assert_called("POST", "/api/v1/remember")
    assert call["form"]["datasetName"] == "agent_sessions"
    assert call["files"] == ["data"]


def test_remember_entry_and_datasets(mock_server):
    status, resp = _post_json(
        f"{mock_server.url}/api/v1/remember/entry",
        {"entry": {"type": "qa"}, "dataset_name": "agent_sessions", "session_id": "s1"},
    )
    assert status == 200 and resp["entry_id"]

    status, first = _post_json(f"{mock_server.url}/api/v1/datasets", {"name": "agent_sessions"})
    assert status == 201 and first["name"] == "agent_sessions"
    status, again = _post_json(f"{mock_server.url}/api/v1/datasets", {"name": "agent_sessions"})
    assert status == 200 and again["id"] == first["id"]


def test_improve_and_status_poll(mock_server):
    status, resp = _post_json(
        f"{mock_server.url}/api/v1/improve",
        {"dataset_name": "agent_sessions", "session_ids": ["s1"], "run_in_background": True},
    )
    assert status == 200 and resp["dataset_id"]

    ds = urllib.parse.quote(resp["dataset_id"])
    _, poll = _http(
        f"{mock_server.url}/api/v1/datasets/status?dataset={ds}&pipeline=cognify_pipeline"
    )
    assert poll[resp["dataset_id"]].endswith("COMPLETED")

    mock_server.set_dataset_status(STATUS_ERRORED)
    _, poll = _http(f"{mock_server.url}/api/v1/datasets/status?dataset={ds}")
    assert poll[resp["dataset_id"]].endswith("ERRORED")

    mock_server.set_improve_response({})  # busy: another improve holds the lock
    _, resp = _post_json(f"{mock_server.url}/api/v1/improve", {"dataset_name": "d"})
    assert resp == {}


def test_credits_overview(mock_server):
    mock_server.set_credits_overview(
        {"tenants": [{"tenantId": "tenant-test", "remainingUsd": 4.2, "spentUsd": 0.8}]}
    )
    _, resp = _http(f"{mock_server.url}/api/v1/billing/credits/overview")
    assert resp["tenants"][0]["remainingUsd"] == 4.2


# -- identity flow ---------------------------------------------------------------


def test_single_principal_key_flow(mock_server):
    base = mock_server.url
    # login (form-urlencoded) -> parseable JWT
    form = urllib.parse.urlencode(
        {"username": "default_user@example.com", "password": "default_password"}
    ).encode()
    status, resp = _http(
        f"{base}/api/v1/auth/login",
        method="POST",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == 200
    jwt = resp["access_token"]
    assert len(jwt.split(".")) == 3

    # no key yet -> GET returns [], POST mints one
    cookie = {"Cookie": f"auth_token={jwt}"}
    assert _http(f"{base}/api/v1/auth/api-keys", headers=cookie)[1] == []
    _, minted = _post_json(f"{base}/api/v1/auth/api-keys", {"name": "owner-bootstrap"}, cookie)
    key = minted["key"]

    # the minted key passes the users/me probe; an invalidated key does not
    assert _http(f"{base}/api/v1/users/me", headers={"X-Api-Key": key})[0] == 200
    mock_server.identity.invalidate_key(key)
    try:
        _http(f"{base}/api/v1/users/me", headers={"X-Api-Key": key})
        raise AssertionError("expected HTTP 401")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401


def test_seeded_owner_key_is_reused(mock_server):
    seeded = mock_server.identity.seed_owner_key("default_user@example.com", "owner-key-1")
    _, resp = _http(
        f"{mock_server.url}/api/v1/auth/login",
        method="POST",
        data=urllib.parse.urlencode(
            {"username": "default_user@example.com", "password": "x"}
        ).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    keys = _http(
        f"{mock_server.url}/api/v1/auth/api-keys",
        headers={"Cookie": f"auth_token={resp['access_token']}"},
    )[1]
    assert keys[0]["key"] == seeded


def test_register_unregister_reflected_in_connections(mock_server):
    base = mock_server.url
    _, reg = _post_json(
        f"{base}/api/v1/agents/register",
        {
            "agent_session_name": "myproj_claude",
            "type": "api",
            "memory_mode": "hybrid",
            "source": "api",
            "session_id": "claude_abc",
        },
    )
    assert reg["registered"] is True and reg["activeAgents"] == 1

    _, conn = _http(f"{base}/api/v1/agents/connections/me")
    assert conn["agent"]["agent_session_name"] == "myproj_claude"
    assert conn["agent"]["status"] == "active"
    assert conn["agent"]["tenant_id"] == "tenant-test"

    _, unreg = _post_json(
        f"{base}/api/v1/agents/unregister", {"agent_session_name": "myproj_claude"}
    )
    assert unreg["activeAgents"] == 0
    assert _http(f"{base}/api/v1/agents/connections/me")[1]["agent"] is None


def test_make_jwt_sub_roundtrip():
    import base64

    payload = make_jwt("user-42").split(".")[1]
    payload += "=" * (-len(payload) % 4)
    assert json.loads(base64.urlsafe_b64decode(payload))["sub"] == "user-42"


# -- isolation -------------------------------------------------------------------


def test_build_env_isolates_and_targets_mock(suite, temp_home, mock_server):
    env = build_env(suite, temp_home, service_url=mock_server.url, api_key="k")
    assert env["HOME"] == str(temp_home)
    assert env["USERPROFILE"] == str(temp_home)
    assert env["COGNEE_BASE_URL"] == mock_server.url
    assert env["COGNEE_PLATFORM_API_URL"] == mock_server.url
    assert env["COGNEE_API_KEY"] == "k"
    assert env[suite.cwd_env] == str(temp_home)
    for key, value in DETERMINISTIC_ENV.items():
        assert env[key] == value


def test_isolated_modules_bind_to_temp_home(
    suite, temp_home, isolated_modules, assert_clean_real_home
):
    config = isolated_modules(suite, "config")
    assert str(config._CONFIG_DIR).startswith(str(temp_home))
    assert config._CONFIG_DIR == config_dir(suite, temp_home)

    common = isolated_modules(suite, "_plugin_common")
    assert common._PLUGIN_DIR == state_dir(suite, temp_home)
    assert str(common._SERVER_READY_MARKER).startswith(str(plugin_root(temp_home)))


def test_isolated_config_reads_mock_url_and_defaults(
    suite, temp_home, mock_server, monkeypatch, isolated_modules
):
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    config = isolated_modules(suite, "config")
    loaded = config.load_config()
    assert loaded["base_url"] == mock_server.url
    assert config.get_dataset(loaded) == suite.default_dataset
    assert loaded["agent_name"] == suite.agent_name


# -- e2e canary ------------------------------------------------------------------


def test_run_hook_session_context_lookup(
    suite, run_hook, mock_server, payloads, assert_clean_real_home
):
    """A real hook runs end-to-end against the mock and exits cleanly."""
    result = run_hook(
        suite,
        "session-context-lookup.py",
        stdin=payloads.user_prompt(prompt="what did we decide?"),
        service_url=mock_server.url,
    )
    assert result.returncode == 0, result.stderr
