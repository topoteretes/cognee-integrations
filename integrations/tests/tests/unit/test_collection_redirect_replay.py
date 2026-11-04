"""Method-preserving redirects (307/308) on the dataset collection route.

Real Cognee servers disagree about the trailing slash on ``/api/v1/datasets``
and answer 307 to the spelling they do not serve — in *opposite* directions:
cloud tenants redirect the bare path to the slashed one, a local server
redirects the slashed path to the bare one. urllib's ``HTTPRedirectHandler``
refuses to replay a POST across a 307 (it raises ``HTTPError`` instead), so
whichever spelling a client hard-codes, it fails against one of the two
server shapes.

That is SDK-622: the clients hard-coded the trailing slash for the cloud's
benefit, so every by-name dataset create against a *local* server surfaced as
a bare ``307`` — which broke ``switch-dataset.py`` entirely, since
``ensure_dataset_ready_via_api`` runs unconditionally on the by-name path.
The suite missed it because the mock server accepted both spellings.

Covers, for every suite:
  * ``_same_origin``: the gate that decides whether a keyed request may be replayed
  * the replay itself, in both redirect directions, through both HTTP helpers
  * the refusals: cross-origin, missing ``Location``, non-redirect statuses
  * a bounded number of hops, so a redirect loop cannot spin
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
from utils.fixtures import DEFAULT_TEST_API_KEY


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


@pytest.fixture
def pc_on_server(pc, mock_server, monkeypatch):
    """``_plugin_common`` pointed at the mock server, for helpers that resolve
    their own base URL (``create_dataset_via_http`` via ``_json_http_request``)."""
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    return pc


# ── the same-origin gate ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "target", "same"),
    [
        # The two spellings of one route on one origin: the case that must replay.
        ("http://localhost:8011/api/v1/datasets/", "http://localhost:8011/api/v1/datasets", True),
        ("https://t.cognee.ai/api/v1/datasets", "https://t.cognee.ai/api/v1/datasets/", True),
        # An explicit default port is the same origin as an implicit one.
        ("https://t.cognee.ai/a", "https://t.cognee.ai:443/a", True),
        ("http://localhost/a", "http://localhost:80/a", True),
        # Everything else is a different origin — these carry X-Api-Key.
        ("https://t.cognee.ai/a", "https://evil.example/a", False),
        ("https://t.cognee.ai/a", "http://t.cognee.ai/a", False),
        ("http://localhost:8011/a", "http://localhost:9999/a", False),
    ],
)
def test_same_origin_gate(pc, source, target, same):
    assert pc._same_origin(source, target) is same


# ── the replay ──────────────────────────────────────────────────────────────


def _post(url, *, key="k", body=None):
    return urllib.request.Request(
        url,
        data=json.dumps(body if body is not None else {"name": "ds"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Api-Key": key},
        method="POST",
    )


@pytest.mark.parametrize("mode", ["to_slashed", "to_bare"])
@pytest.mark.parametrize("spelling", ["/api/v1/datasets", "/api/v1/datasets/"])
def test_dataset_create_survives_either_redirect_direction(pc, mock_server, mode, spelling):
    """Both spellings work against both server shapes, POST body intact."""
    mock_server.set_collection_redirect(mode)

    with pc.urlopen_following_307(
        _post(f"{mock_server.url}{spelling}", body={"name": "ds-redirect"}),
        timeout=5.0,
        context=None,
    ) as resp:
        assert resp.status in (200, 201)
        created = json.loads(resp.read().decode("utf-8"))

    # The replayed request kept its method AND its body: the dataset is named.
    assert created.get("name") == "ds-redirect"
    mock_server.assert_called("POST", "/api/v1/datasets", name="ds-redirect")


@pytest.mark.parametrize("mode", ["to_slashed", "to_bare"])
def test_ensure_dataset_ready_survives_either_redirect_direction(
    suite, isolated_modules, mock_server, mode
):
    """``config.ensure_dataset_ready_via_api`` — the path switch-dataset.py uses."""
    import asyncio

    config = isolated_modules(suite, "config")
    mock_server.set_collection_redirect(mode)

    # Must not raise: a bare 307 here is what blocked every by-name switch.
    asyncio.run(
        config.ensure_dataset_ready_via_api(mock_server.url, DEFAULT_TEST_API_KEY, "ds-ensure")
    )
    mock_server.assert_called("POST", "/api/v1/datasets", name="ds-ensure")

    # Runs unconditionally on every switch, so it has to stay idempotent.
    asyncio.run(
        config.ensure_dataset_ready_via_api(mock_server.url, DEFAULT_TEST_API_KEY, "ds-ensure")
    )


@pytest.mark.parametrize("mode", ["to_slashed", "to_bare"])
def test_create_dataset_via_http_survives_either_redirect_direction(
    pc_on_server, mock_server, mode
):
    """The other client of this route, reached through ``_json_http_request``."""
    mock_server.set_collection_redirect(mode)

    row = pc_on_server.create_dataset_via_http(DEFAULT_TEST_API_KEY, "ds-helper")

    assert row.get("name") == "ds-helper"
    assert row.get("id")


# ── the refusals ────────────────────────────────────────────────────────────


def test_cross_origin_redirect_is_not_followed(pc, mock_server, monkeypatch):
    """A keyed request must never be replayed to another host."""
    replayed: list[str] = []
    real_urlopen = urllib.request.urlopen

    def spy(req, *args, **kwargs):
        replayed.append(req.full_url if hasattr(req, "full_url") else str(req))
        url = replayed[-1]
        if url.startswith(mock_server.url):
            raise urllib.error.HTTPError(
                url, 307, "Temporary Redirect", {"Location": "https://evil.example/steal"}, None
            )
        return real_urlopen(req, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", spy)

    with pytest.raises(urllib.error.HTTPError) as raised:
        pc.urlopen_following_307(
            _post(f"{mock_server.url}/api/v1/datasets", key="secret-key"),
            timeout=5.0,
            context=None,
        )

    # The original 307 reaches the caller, and the other host was never called.
    assert raised.value.code == 307
    assert not any("evil.example" in url for url in replayed)


def test_redirect_without_location_is_not_followed(pc, monkeypatch):
    def spy(req, *args, **kwargs):
        raise urllib.error.HTTPError(req.full_url, 307, "Temporary Redirect", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", spy)

    with pytest.raises(urllib.error.HTTPError) as raised:
        pc.urlopen_following_307(_post("http://localhost:8011/x"), timeout=5.0, context=None)
    assert raised.value.code == 307


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409, 500, 503])
def test_non_redirect_statuses_pass_through_untouched(pc, monkeypatch, code):
    calls: list[str] = []

    def spy(req, *args, **kwargs):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, code, "boom", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", spy)

    with pytest.raises(urllib.error.HTTPError) as raised:
        pc.urlopen_following_307(_post("http://localhost:8011/x"), timeout=5.0, context=None)

    assert raised.value.code == code
    assert len(calls) == 1, "a non-redirect status must not be retried"


def test_redirect_loop_is_bounded(pc, monkeypatch):
    """A server that redirects forever must not spin the hook."""
    calls: list[str] = []

    def spy(req, *args, **kwargs):
        calls.append(req.full_url)
        # Always point at the other spelling: an endless ping-pong.
        target = req.full_url.rstrip("/") if req.full_url.endswith("/") else req.full_url + "/"
        raise urllib.error.HTTPError(
            req.full_url, 307, "Temporary Redirect", {"Location": target}, None
        )

    monkeypatch.setattr(urllib.request, "urlopen", spy)

    with pytest.raises(urllib.error.HTTPError):
        pc.urlopen_following_307(
            _post("http://localhost:8011/api/v1/datasets"), timeout=5.0, context=None
        )

    assert len(calls) <= pc._MAX_REDIRECT_REPLAYS + 1
