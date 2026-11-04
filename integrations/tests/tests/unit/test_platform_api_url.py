"""The billing host resolution (_plugin_common._platform_api_url).

Cloud sessions talk to a per-tenant data-plane host which serves
recall/remember/improve but has NO billing routes — asking it for the credits
overview 404s. The billing routes live only on the platform API, which accepts
the same tenant COGNEE_API_KEY. This is pure string resolution, so it stays a
unit test; the "billing actually goes to that host" claim is covered in
integration/test_credits_refresh.py.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def pc(suite, isolated_modules):
    return isolated_modules(suite, "_plugin_common")


def test_defaults_to_the_platform_api_host(pc):
    # The harness scrubs COGNEE_PLATFORM_API_URL, so this is the true default.
    assert pc._platform_api_url() == pc._PLATFORM_API_URL_DEFAULT
    assert pc._PLATFORM_API_URL_DEFAULT.startswith("https://")


def test_env_override_wins_and_is_normalized(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_PLATFORM_API_URL", "  https://platform.example/  ")
    assert pc._platform_api_url() == "https://platform.example"
